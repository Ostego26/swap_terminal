"""Compose the four XMR-swap components into one protocol, as decisions with seeded inputs.

Role: submodule. It holds the DECISIONS of a script-chain <-> Monero adaptor swap --
      is this share in range, is this counterparty share admissible, is this DLEQ proof
      about the keys I hold, may the redeem pre-signature be released yet, is this
      recovered scalar the one the address was built from, is this timelock ordering
      safe -- as functions callable with seeded inputs and no chain (rule 10). It
      SEQUENCES nothing and reaches no network. `monero_swap.py` at the project root is
      the file that runs it.
Reads: nothing. No database, no RPC endpoint, no environment variable, no file. The
      cross-curve DLEQ arrives as an already-constructed `dleq_helper.DleqHelper`, passed
      in rather than created here, so every function below is testable without a
      subprocess and so this module never decides which binary runs.
Writes: nothing. Not to disk, not to a log. There is no logger in this file on purpose --
      see NOTHING HERE PRINTS, below.
Can move funds: no. It builds no transaction, signs nothing that reaches a chain, and
      broadcasts nothing. It is FUND-ADJACENT in the strongest sense this tree has: the
      scalars passing through it are Monero spend-key shares, and a wrong answer from
      `verify_share_commitment` or `recover_counterparty_spend_share` is how a
      counterparty takes the script-chain coin and leaves the XMR locked to a key nobody
      holds.
Mainnet-safe: NO. Not because it can reach mainnet -- it cannot reach anything -- but
      because the cryptography underneath it is UNAUDITED. There is no audited
      implementation of the cross-curve DLEQ construction in any language.
      docs/dleq_cross_curve_design.md section 7 argues against building this at all and
      every one of its arguments still stands; this file is section 6 stage 5 carried
      out, not an answer to section 7.
Live-safe: yes to import and to call. No chain, no lock, no state, no side effects.

WHAT THIS FILE IS, AND WHAT IT IS NOT

`docs/monero_swap_protocol.md` is the design. Read it first; it carries the per-party,
per-step analysis of what each side can take and what each loses by walking away, and
every refusal below exists because of a specific row in it. This file is the decisions
from that document and nothing else -- no orchestration, no transaction building, no
sequencing.

IT IS NOT A COMPLETE SWAP, AND THE MISSING PIECE IS NOT IN THIS FILE'S LAYER.
Measured 2026-09-27 by reading `modules/atomic_htlc_scripts.build_htlc_redeem_script()`:
both branches of this repo's HTLC end in a single-key `OP_CHECKSIG`, and
`modules/htlc_spend.hashlock_script_sig()` takes a signature the claimer produced with
their own key. So an adaptor signature has NO PURCHASE on the existing HTLC -- the whole
mechanism requires the spender to be unable to sign alone, so that the only signature
available to them is the one they must complete with the scalar. The script chain
therefore needs a 2-of-2 lock plus a four-transaction pre-signed chain (redeem, cancel,
refund, punish) that DOES NOT EXIST in this tree. That is a fifth component, it was not
on the list of four, and it is named work rather than a baseline (rule 19). The design
document's section 2 specifies it.

What that means for reading this file: every function here is real and tested, and
together they are the cryptographic half of a swap whose transaction half is absent.

NOTHING HERE PRINTS, AND NO EXCEPTION QUOTES A SCALAR

Every value that flows through this module on the private side is a Monero spend-key or
view-key share. So:

  - `PrivateShares.__repr__` and `__str__` are overridden to redact. The default dataclass
    repr would put both shares into any traceback, any `print(shares)` a future reader
    adds while debugging, any `logger.info(f"{shares}")`, and any pytest assertion failure
    message. A redacting repr is not decoration: the default one is a leak that costs
    nothing to write and is invisible until it has already happened.
  - No refusal below quotes the value it refused. `require_share_in_range` reports a BIT
    LENGTH, which is what a caller needs to fix a sampling bug and is 252 bits short of
    being the key. `modules/dleq_helper.witness_from_int` makes the same choice for the
    same reason and says so.
  - There is deliberately no module-level logger. A file whose arguments are key shares
    should not have a logging call available to be reached for.

`tests/test_monero_swap_protocol.py` asserts the redaction behaviorally -- it builds
shares from distinguishable values and asserts those values appear in neither `repr` nor
`str` nor an f-string -- rather than asserting that the source contains an `__repr__`.

WHY THE SHARE BOUND IS 2^252 AND NOT THE ED25519 GROUP ORDER

`chains/monero_keys.shared_private_spend_key` accepts any share in `[1, l)` where
`l = 2^252 + 27742317777372353535851937790883648493`, which is correct for Monero
arithmetic on its own. This protocol is TIGHTER, at `[1, 2^252)`, and the difference is
not a margin -- it is the premise of the cross-curve proof.

A share at or above `2^252` cannot be proven equal across the two curves at all: the
statement is only well-formed below the smaller group order, and 252 is
`min(ed25519 252, secp256k1 255)` -- read from `go-dleq`'s `BitSize()` and reported by the
running helper's own `version` response as `bit_count: 252`. A prover handed a larger
value must refuse rather than reduce, because reducing commits to two different integers
on the two curves. `require_share_in_range` below is that refusal, and it is stricter
than `monero_keys`' l-bound on purpose. The difference is named at both sites (rule 8).

THE SUM IS NEVER PROVEN AND NEVER COMPARED ACROSS CURVES

Measured in this container 2026-09-27: of 2000 random share pairs from `[1, 2^252)`, 961
-- 48.0% -- sum to at least `l`. For those pairs the secp256k1 sum point and the ed25519
sum point commit to DIFFERENT INTEGERS, with no error raised anywhere, because the sum
reduces mod `l` on one curve and does not reduce mod `n` on the other.
docs/dleq_cross_curve_design.md section 3.2 is the full statement; the consequence for
this file is three absolute rules:

  - each share gets its OWN proof and its OWN adaptor point. `commit_shares` proves one
    share; there is no function here that proves a sum.
  - nothing adds two adaptor points. Search this file for `+` on a secp256k1 point and
    you will not find one.
  - the addition happens on the ed25519 side only, in `reconstruct_spend_key`, after the
    missing share is known as an integer.

THE THREE HAZARDS, AND THE FUNCTION THAT REFUSES EACH

The design document names them; each is a function here so it can be called with seeded
inputs instead of being a paragraph somebody has to remember:

  hazard 1  the first funder must stay online in [T1, T2] or lose its coin to the punish
            path.                              -> assert_timelock_ordering
  hazard 2  the Monero leg must not be funded without enough time left before T1 to
            confirm, unlock, and redeem.        -> assert_timelock_ordering
  hazard 3  THE ONE BOTH-LEGS OUTCOME. Broadcasting the redeem publishes the scalar; if
            that transaction is then re-orged out and the cancel confirms instead, the
            counterparty holds both shares AND its own coin, and takes both legs. Bounded
            only by confirmation depth before T1.
                                               -> redeem_is_safe_to_broadcast
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from chains.monero_keys import (
    public_key_for_share,
    require_public_share,
    shared_address,
    shared_private_spend_key,
    shared_public_key,
)

# format_duration is the ONLY presentation concern this module imports, and it is used
# in exactly two places: the text of a refusal a human reads. Rule 6's boundary is
# report-versus-interface, so every number in this file is seconds and the conversion to
# µfn happens at the message, never in the arithmetic. It was a function-local import
# until ruff's PLC0415 flagged it; the comment that justified keeping it local is now this
# one, because an import-time side effect is what the rule is guarding against and
# microfortnights.py has none (it defines a constant and two functions).
from microfortnights import format_duration
from modules import adaptor_ecdsa
from modules.dleq_helper import DleqHelperError, witness_from_int
from modules.dleq_proof_format import WITNESS_BITS
from modules.ed25519_group import scalar_base_mul
from modules.htlc_timelock import SECONDS_PER_BLOCK

# The share bound, derived from the wire format's own bit count rather than spelled as
# 252 here. dleq_proof_format.WITNESS_BITS is where that number lives, and a second
# literal copy of it is rule 8's failure with a delay on it.
SHARE_UPPER_BOUND = 1 << WITNESS_BITS

# Monero's target block interval, in seconds.
#
# THIS IS DELIBERATELY NOT A ROW IN modules/htlc_timelock.SECONDS_PER_BLOCK, and the
# divergence is named here and there because rule 8 asks for exactly that. That table
# feeds `timelock_blocks()` and `contract_locktime()`, which turn a policy in hours into
# an absolute BLOCK HEIGHT for an `OP_CHECKLOCKTIMEVERIFY` operand. Monero has no script,
# so it has no CLTV, no height to compute, and no timelock of any kind -- adding "XMR"
# there would make `contract_locktime("XMR", role, tip)` return a plausible-looking
# height for a script that cannot exist, which is a worse failure than the missing row.
#
# The only thing Monero's block target is needed for in this protocol is converting the
# output lock below into a wall-clock wait, so that it can be compared against a
# script-chain timelock IN SECONDS (rule 6's unit boundary, and atomic_swap.py's
# assert_ordering incident: block counts are comparable within one chain and are a
# category error across two).
XMR_SECONDS_PER_BLOCK = 120

# An ordinary Monero output is not spendable for 10 blocks. A COINBASE output locks for
# 60, which is why monero_regtest.py has to mine past it before it can spend.
#
# This is the term that is easiest to leave out of a timelock calculation, and leaving it
# out is hazard 2: the funder computes "confirmations will take N seconds" and funds with
# N seconds of margin, then cannot redeem for another 20 minutes. Measured on the
# operator's host 2026-09-27, and it is the measurement that refutes the published spec:
# a transfer with 3 confirmations and unlock_time=0 still reported locked=True and
# contributed 0 to unlocked_balance. `locked=True` means NOT spendable, which is the
# opposite of what the documentation says.
XMR_OUTPUT_LOCK_BLOCKS = 10

# Hex string lengths, so a malformed commitment is refused by length before it reaches
# curve arithmetic that would raise something less legible.
SECP256K1_POINT_HEX_LEN = 66
ED25519_POINT_HEX_LEN = 64


# The nettypes monerod reports in `get_info` that are NOT a real chain. Regtest is spelled
# "fakechain" there -- it is its own nettype rather than a mode over another one, which is
# also why `generateblocks` is gated to it inside monerod (monero_regtest.py records the
# source line).
#
# ASKED, NEVER INFERRED FROM A PORT. A port is a convention and a convention is not a
# check: 18081 and 38081 are one config line apart, and swap_terminal/config.py records
# what happened the last time a port was trusted. The same rule atomic_swap.py's
# `chain_name` follows for the script chain, for the same reason.
XMR_TEST_NETTYPES = frozenset({"fakechain", "stagenet", "testnet"})

# Which ADDRESS PREFIX a given daemon nettype uses. This is not the identity map and the
# one row that is not is a live trap.
#
# REGTEST ADDRESSES CARRY THE MAINNET PREFIX BYTE. cryptonote_config.h:361 has
# `case FAKECHAIN: return mainnet;`, so a wallet on a regtest chain produces addresses that
# decode as mainnet/primary -- byte 18, not a regtest byte, because there is no regtest
# byte. Two consequences, both measured on the operator's host 2026-09-27:
#
#   - a shared address for a regtest swap must be BUILT with mainnet prefixes, or the
#     wallet will not recognize it;
#   - `validate_address` reports the ADDRESS FORMAT's network, not the DAEMON's. Asking it
#     which network you are on gets the wrong answer confidently. Ask monerod's `get_info`
#     for `nettype` instead, which is what `monero_network_is_test` below takes.
#
# A driver that read "mainnet" off a regtest address and refused to proceed would be
# refusing correctly-built work, and one that read "mainnet" off a MAINNET address and
# proceeded because it assumed regtest would be the other error. The nettype is the
# authority; the prefix is derived from it here.
XMR_ADDRESS_NETWORK_BY_NETTYPE = {
    "fakechain": "mainnet",
    "stagenet": "stagenet",
    "testnet": "testnet",
    "mainnet": "mainnet",
}


class ProtocolError(RuntimeError):
    """A refusal. Every one of these is a named protocol violation, never a retry hint."""


class ShareRangeError(ProtocolError):
    """A private share outside `[1, 2^252)`. Raised without quoting the share."""


class ShareRejected(ProtocolError):
    """A counterparty's committed share is not admissible, or its proof is not about it."""


@dataclass(frozen=True)
class PrivateShares:
    """One party's two Monero private shares. NEVER printed, NEVER stored, NEVER logged.

    The spend share is the value the whole swap turns on: whoever holds both spend shares
    can spend the locked XMR. The view share is exchanged in the clear with the
    counterparty -- both parties need the full private view key `v = v_a + v_b` to watch
    the lock address -- but it is still key material and it still never reaches a log.

    THE REDACTING repr IS A SECURITY DECISION AND IS TESTED AS ONE. A frozen dataclass's
    generated `__repr__` interpolates every field, so the default would put both shares
    into any traceback that mentions this object, any `print()` a future reader adds while
    debugging, and any pytest assertion-failure message. That is a leak which costs one
    method to prevent and is invisible until after it has happened.
    """

    spend: int
    view: int

    def __post_init__(self) -> None:
        require_share_in_range(self.spend, "spend share")
        require_share_in_range(self.view, "view share")

    def __repr__(self) -> str:
        return "PrivateShares(spend=<redacted>, view=<redacted>)"

    __str__ = __repr__


@dataclass(frozen=True)
class ShareCommitment:
    """Everything one party PUBLISHES about its shares. No private value appears here.

    This is the message that crosses the wire at step 0 of the protocol, minus the view
    SHARE itself (which is private and travels beside it). Four fields:

      adaptor_point   `s * G` on secp256k1, compressed SEC1, 33 bytes as 66 hex
                      characters. This is the `Y` the counterparty's redeem or refund
                      pre-signature is made under.
      spend_public    `s * B` on ed25519, compressed, 32 bytes as 64 hex characters. This
                      is the half that goes into the lock address.
      view_public     `v * B`, same encoding.
      dleq_proof      the cross-curve proof that `adaptor_point` and `spend_public` have
                      the SAME discrete logarithm. 64,966 to 64,968 bytes; it is not a
                      constant, because the secp256k1 signature inside it is DER-encoded
                      and runs 70 to 72 bytes.

    Measured 2026-09-27: `go-dleq`'s two claimed keys are `x * G` and `x * B` against the
    STANDARD basepoints (its `prove.go` computes `XA = curveA.ScalarBaseMul(xA)` and uses
    `AltBasePoint()` only as the BLINDER generator). So the proof's own commitments are
    directly the adaptor point and the Monero spend-share public key, with no conversion
    step -- `commitment_secp256k1` came back byte-identical to
    `adaptor_ecdsa.point_to_bytes(public_key_point(x))` and `commitment_ed25519`
    byte-identical to `monero_keys.public_key_for_share(x)`. That is the join, and it
    being an identity rather than a mapping is the single most useful fact in this file.
    """

    adaptor_point: str
    spend_public: str
    view_public: str
    dleq_proof: bytes


@dataclass(frozen=True)
class TimelockPlan:
    """The timing parameters a swap's two absolute script-chain timelocks are checked against.

    Grouped into one object rather than passed as eight arguments because ruff's PLR0913
    was right that eight wanted grouping, and because rule 12 says to answer a complexity
    finding by extracting rather than by raising a ceiling. It is also not only a count
    fix: a plan that travels as one value cannot be assembled inconsistently at two call
    sites, which is what a loose argument list invites.

    Every field is in SECONDS, and that is the whole point (rule 6's interface boundary:
    seconds where arithmetic happens, µfn only on the way out to a reader). T1 and T2 are
    block HEIGHTS on the script chain, and the number of blocks between them is not
    comparable to anything on the Monero side -- Gridcoin at 90s a block, Litecoin at
    150, Bitcoin at 600 and Monero at 120 make four different exchange rates between
    blocks and time. atomic_swap.py's assert_ordering carries the incident from getting
    this wrong across two chains; this protocol has three.

      s_chain_asset            "GRC", "BTC" or "LTC" -- decides how long a confirmation
                               takes, via htlc_timelock.SECONDS_PER_BLOCK.
      seconds_until_cancel     T1 minus now.
      seconds_until_punish     T2 minus now. Must exceed the above.
      xmr_confirmations        how many Monero confirmations the funder waits for before
                               treating the lock as real.
      redeem_confirmations     script-chain confirmations the redeem must reach.
      cancel_confirmations     script-chain confirmations the cancel must reach.
      offline_allowance_seconds  the longest the first funder may be unresponsive between
                               T1 and T2 without losing its coin to the punish path. This
                               is hazard 1 expressed as a number.
      margin_seconds           slack required on every comparison. A comparison with zero
                               margin is a comparison that a single slow block breaks.
    """

    s_chain_asset: str
    seconds_until_cancel: int
    seconds_until_punish: int
    xmr_confirmations: int
    redeem_confirmations: int
    cancel_confirmations: int
    offline_allowance_seconds: int
    margin_seconds: int


@dataclass(frozen=True)
class RehearsalResult:
    """What an offline, both-sides-played rehearsal of the cryptography established.

    NO PRIVATE VALUE IS CARRIED HERE. The rehearsal holds both parties' shares while it
    runs, and it reduces them to the booleans below before returning, so that a caller --
    including a caller that prints its result, which `monero_swap.py` does -- cannot
    expose a share by reporting on a rehearsal.

    `spend_key_opens_lock` is the property the regtest sweep proved on a chain and is the
    one that matters most: the reconstructed private spend key's public key equals the
    summed public spend key the lock address was built from. It is checked here as a
    point comparison rather than inferred from the addition having not raised.
    """

    lock_address: str
    proof_bytes_initiator: int
    proof_bytes_participant: int
    dleq_verified_both_ways: bool
    presignature_verified: bool
    recovered_share_matches_commitment: bool
    spend_key_opens_lock: bool
    low_s_negation_exercised: bool


def require_share_in_range(value: int, name: str = "share") -> int:
    """A private share must be an int in `[1, 2^252)`, and the value is NEVER quoted.

    Three refusals, and they are separate because they mean different things to whoever
    has to fix the call:

      not an int   a programming error, usually a hex string or bytes that was meant to be
                   decoded first. The TYPE is named because that is the fix.
      zero         a protocol attack rather than a typo, and it gets its own sentence for
                   the reason `monero_keys._checked_share` gives it one: a share of zero
                   makes the shared spend key equal the OTHER share alone, so whoever
                   accepted the zero has handed the counterparty the whole key. The
                   corresponding public share is the identity, and `require_public_share`
                   refuses that on the other side of the wire.
      out of range a sampling bug. The BIT LENGTH is reported and the value is not: a bit
                   count is exactly what is needed to see that the sampling is wrong, and
                   it is 252 bits short of being the key. dleq_helper.witness_from_int
                   makes the same trade for the same reason.

    Tighter than `monero_keys` by design: that module's bound is the ed25519 group order
    `l`, which is correct for Monero arithmetic alone, and this one is `2^252`, which is
    what the cross-curve proof's statement requires. See the module docstring.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        # bool is an int subclass, and `True` would otherwise pass as the share 1. That is
        # not a hypothetical shape of bug: a flag threaded through the wrong parameter is
        # exactly how a one-scalar share arrives, and it would produce a valid proof.
        raise ShareRangeError(f"{name} must be an int, got {type(value).__name__}")
    if value == 0:
        raise ShareRangeError(
            f"{name} is zero. The shared spend key would then equal the other party's share "
            f"alone, so one side would hold the whole key -- refused as a protocol violation, "
            f"not corrected"
        )
    if not 0 < value < SHARE_UPPER_BOUND:
        raise ShareRangeError(
            f"{name} must be in [1, 2^{WITNESS_BITS}); this one is {value.bit_length()} bits and "
            f"{'negative' if value < 0 else f'at or above 2^{WITNESS_BITS}'}. Above that bound it "
            f"is not provably the same scalar on both curves, which is the premise of the swap"
        )
    return value


def sample_shares() -> PrivateShares:
    """Two fresh shares from `secrets`, uniform on `[1, 2^252)`.

    `secrets.randbelow` and not `random`: these are key shares, and ruff's S311 exists
    for precisely this call. The `or 1` is not a fudge -- `randbelow(2^252)` can return 0
    with probability 2^-252, and a zero share is refused downstream, so the alternative to
    mapping it is a function that raises once per 10^76 calls.

    Entropy cost of the 252-bit restriction, computed in this container: `2^252 / l` is
    `1 - 3.8e-39`, so restricting to 252 bits gives up essentially nothing relative to the
    ed25519 key space. Generic discrete-log security is `2^126` either way. There is no
    security argument against the restriction and no reason to be clever about it.
    """
    return PrivateShares(
        spend=secrets.randbelow(SHARE_UPPER_BOUND) or 1,
        view=secrets.randbelow(SHARE_UPPER_BOUND) or 1,
    )


def commit_shares(helper, shares: PrivateShares) -> ShareCommitment:
    """Prove one party's spend share across the curves and publish the public halves.

    `helper` is a live `dleq_helper.DleqHelper`, passed in rather than constructed, so
    this module never decides which binary runs and so a caller can substitute a fake in a
    test (rule 10: the decision is here, the process is not).

    THE PROOF IS ABOUT THE SPEND SHARE ONLY. The view share needs no cross-curve proof
    because nothing on the script chain is ever pre-signed under it -- it exists only so
    both parties can watch the lock. Proving it would be 64 KiB of work for a statement
    nobody checks.

    The helper's returned commitments are CROSS-CHECKED here against this repo's own
    derivations rather than trusted. That is two scalar multiplications against a 64 KiB
    proof, and it is what makes the `expect_*` arguments on the far side meaningful: if the
    helper and this repo disagreed about the encoding, a counterparty's verify would fail
    for a reason nobody could diagnose from either end. Measured 2026-09-27 as
    byte-identical over 12 of 12 random trials.
    """
    require_share_in_range(shares.spend, "spend share")
    result = helper.prove(witness_from_int(shares.spend))

    expected_adaptor = adaptor_ecdsa.point_to_bytes(
        adaptor_ecdsa.public_key_point(shares.spend)
    ).hex()
    expected_spend_public = public_key_for_share(shares.spend).hex()
    if result.commitment_secp256k1.lower() != expected_adaptor:
        raise ProtocolError(
            "the DLEQ helper's secp256k1 commitment does not match this repo's own derivation "
            "of the same share's public key. The two disagree about the encoding or the "
            "generator, and a proof built on that disagreement would be unverifiable by a "
            "counterparty for no discoverable reason"
        )
    if result.commitment_ed25519.lower() != expected_spend_public:
        raise ProtocolError(
            "the DLEQ helper's ed25519 commitment does not match this repo's own derivation of "
            "the same share's public key -- same disagreement, other curve"
        )

    return ShareCommitment(
        adaptor_point=result.commitment_secp256k1.lower(),
        spend_public=result.commitment_ed25519.lower(),
        view_public=public_key_for_share(shares.view).hex(),
        dleq_proof=result.proof,
    )


def verify_share_commitment(helper, commitment: ShareCommitment) -> None:
    """Accept or REFUSE a counterparty's committed share. This is the gate the swap rests on.

    Four checks, in the order that makes the cheapest refusal first, and every one of them
    is a way a counterparty takes the script-chain coin and leaves the XMR locked to a key
    nobody holds:

      1. LENGTHS. A hex string of the wrong length is refused before any curve arithmetic,
         so the error names the field rather than surfacing from a decoder.
      2. THE SPEND SHARE IS AN ADMISSIBLE ED25519 POINT. Canonical encoding, on the curve,
         not the identity, torsion-free -- all four via
         `monero_keys.require_public_share`, which is the ONE implementation of that check
         in this tree (rule 8) and carries the reasoning for each. The torsion case is the
         one that would be tempting to skip: a share with a small-order component is on the
         curve and encodes canonically, and several distinct encodings agree with it, so the
         share does not pin one point.
      3. THE VIEW SHARE, same four checks. It decides nothing about spending, but a view
         share that does not pin one point means the two parties can end up watching
         different addresses, and "I cannot see the lock" is indistinguishable from "the
         lock was never funded".
      4. THE PROOF IS ABOUT THESE KEYS. `helper.verify(..., expect_secp256k1=...,
         expect_ed25519=...)`, never `verify_any`.

    CHECK 4 IS THE ONE WHOSE ABSENCE LOOKS EXACTLY LIKE A WORKING VERIFIER. A DLEQ proof
    says "the two points I commit to have the same discrete logarithm". It says nothing
    about WHICH points. A caller that verifies a proof and then builds an address from
    keys it got separately has verified a true statement about somebody else's keys and
    learned nothing about its own. `dleq_helper.commitments_match` is the comparison and
    `verify`'s `expect_*` arguments are required rather than optional for this reason;
    `verify_any` exists as a separately named call for the case where the caller genuinely
    does not know the keys yet, and this is not that case.

    Raises ShareRejected on every failure. It does not return a boolean, because a caller
    that forgets to check a boolean proceeds, and a caller that forgets to catch an
    exception stops.
    """
    if len(commitment.adaptor_point) != SECP256K1_POINT_HEX_LEN:
        raise ShareRejected(
            f"the adaptor point must be {SECP256K1_POINT_HEX_LEN} hex characters (a 33-byte "
            f"compressed secp256k1 point), got {len(commitment.adaptor_point)}"
        )
    for field, value in (("spend", commitment.spend_public), ("view", commitment.view_public)):
        if len(value) != ED25519_POINT_HEX_LEN:
            raise ShareRejected(
                f"the {field} public share must be {ED25519_POINT_HEX_LEN} hex characters (a "
                f"32-byte compressed ed25519 point), got {len(value)}"
            )

    for field, value in (("spend", commitment.spend_public), ("view", commitment.view_public)):
        try:
            raw = bytes.fromhex(value)
        except ValueError as error:
            raise ShareRejected(f"the {field} public share is not hex: {error}") from error
        # require_public_share raises MoneroKeyError, a ValueError subclass, with the reason
        # already written. Re-raising as ShareRejected keeps one exception type at this
        # module's boundary while preserving that reason verbatim -- the caller needs to know
        # WHICH of the four refusals fired, and the chained cause carries it.
        try:
            require_public_share(raw, f"{field} public share")
        except ValueError as error:
            raise ShareRejected(f"the {field} public share is not admissible: {error}") from error

    try:
        verdict = helper.verify(
            commitment.dleq_proof,
            expect_secp256k1=commitment.adaptor_point,
            expect_ed25519=commitment.spend_public,
        )
    except DleqHelperError as error:
        raise ShareRejected(f"the cross-curve DLEQ proof was not accepted: {error}") from error
    if not verdict.verified:
        raise ShareRejected(
            f"the cross-curve DLEQ proof does not establish that the adaptor point and the "
            f"spend public share have the same discrete logarithm: "
            f"{verdict.reason or 'the verifier returned false with no reason'}. Without that, "
            f"completing the adaptor signature reveals a scalar that need not open the lock"
        )


def lock_address(network: str, mine: ShareCommitment, theirs: ShareCommitment) -> str:
    """The Monero address the swap locks to, from the four PUBLIC halves.

    Built from the commitment objects, which is the point: the bytes that go in are the
    same bytes `verify_share_commitment` checked the proof against. A function that took
    loose hex strings would let a caller verify one copy of a share and build the address
    from another, and the two would differ only in the case somebody was attacking it.

    Both parties call this and must get the SAME string. Point addition commutes, so
    `lock_address(net, mine, theirs)` and `lock_address(net, theirs, mine)` are equal --
    asserted in the test rather than left to the reader, because the whole protocol rests
    on the two sides agreeing on one address, and an address they disagree about is money
    sent somewhere one of them cannot see.

    There is no Monero multisig here and there is none anywhere in this protocol. This is
    an ordinary address whose private spend key happens to be a sum, which is the fact
    that makes the swap implementable at all -- no CLSAG work, no ring-signature code in
    Python, and the reconstructed key imports into `monero-wallet-rpc` with
    `generate_from_keys`.
    """
    return shared_address(
        network,
        bytes.fromhex(mine.spend_public),
        bytes.fromhex(theirs.spend_public),
        bytes.fromhex(mine.view_public),
        bytes.fromhex(theirs.view_public),
    )


def redeem_presignature_may_be_released(
    *, xmr_lock_confirmed: bool, xmr_lock_unlocked: bool
) -> tuple[bool, str]:
    """May the redeem pre-signature be sent to the counterparty yet? BOTH conditions, or no.

    THIS WITHHOLDING IS A SECURITY PROPERTY AND NOT AN OPTIMIZATION, and it is the single
    thing standing between the first funder and a total loss during steps 1 through 3 of
    the protocol.

    The party receiving script-chain coin holds its own Monero spend share. The moment it
    has the redeem pre-signature it can complete it and take the coin. Released at setup,
    it takes that coin having sent no XMR at all: the other side recovers the scalar from
    the broadcast, reconstructs the spend key, opens the lock address, and finds it empty.
    So the release is gated on an OBSERVATION OF THE MONERO CHAIN, never on a message from
    the counterparty asking for it, and nothing may compute the pre-signature earlier "to
    have it ready" -- a value that exists in a process is a value that can be sent by the
    next line somebody writes.

    TWO CONDITIONS AND NOT ONE, because on Monero they are genuinely different and the
    published documentation has the second one backwards. Measured on the operator's host
    2026-09-27: a transfer with 3 confirmations and `unlock_time=0` still reported
    `locked=True` and contributed 0 to `unlocked_balance`. `locked=True` means NOT
    spendable. An ordinary output locks for 10 blocks. So "confirmed" does not imply
    "spendable", and releasing on confirmations alone releases against a lock the funder
    could still be unable to use.

    Returns a reason with the verdict rather than a bare boolean, so the refusal can be
    printed (rule 14: state what the number means, next to the number).
    """
    if not xmr_lock_confirmed:
        return False, (
            "the Monero lock is not confirmed. Releasing the redeem pre-signature now lets the "
            "counterparty take the script-chain coin having funded nothing"
        )
    if not xmr_lock_unlocked:
        return False, (
            "the Monero lock is confirmed but still locked=True, so it is NOT spendable -- an "
            "ordinary output locks for 10 blocks. Releasing now would hand over the redeem "
            "against a lock that cannot yet be swept"
        )
    return True, "the Monero lock is confirmed and spendable"


def recover_counterparty_spend_share(
    pre_signature, signature: tuple[int, int], expect_spend_public: str
) -> int:
    """The counterparty's spend share, read out of the signature they broadcast. Then CHECKED.

    This is the step the whole swap converts into money. The counterparty completed an
    adaptor pre-signature to take their leg, and the act of broadcasting it published a
    complete ECDSA signature from which the scalar falls out.

    TWO CHECKS, AND THE SECOND IS NOT REDUNDANT.

    `adaptor_ecdsa.recover_adaptor_secret` guarantees only that the returned scalar
    multiplies the secp256k1 generator to the pre-signature's adaptor point. The ed25519
    half of the claim -- that the same scalar multiplies the ed25519 basepoint to the
    public share the lock address was built from -- comes from the cross-curve DLEQ, which
    was verified at a different time, in a different function, possibly in a different
    process run. Re-checking it here costs one scalar multiplication and catches every way
    that link can be broken in between: a skipped verification, a verification against a
    different key, a mixed-up pre-signature object, or a share whose commitment was
    replaced after it was checked.

    It is also the check that makes the failure LOUD instead of silent. Without it, a
    scalar that opens nothing is handed to `reconstruct_spend_key`, which adds it happily,
    and the first sign of trouble is a reconstructed wallet with a zero balance and no
    explanation -- which reads as an operational fault rather than as a broken proof.
    docs/dleq_cross_curve_design.md section 7 argument 4 is precisely this: "Failure is
    total, delayed, and looks like an operational fault."

    Measured 2026-09-27 over 12 random trials: the recovered integer equals the witness
    exactly in 12 of 12, and its ed25519 public key equals the DLEQ-proven commitment in
    12 of 12. The `-Y` branch of `recover_adaptor_secret` -- the one that exists because
    `adapt()` negates `s` for BIP62 low-S -- fired in 6 of those 12, so it is not a
    curiosity: a recovery that dropped it would fail on about half of all real swaps.
    """
    recovered = adaptor_ecdsa.recover_adaptor_secret(pre_signature, signature)
    if recovered is None:
        raise ProtocolError(
            "the broadcast signature does not correspond to this pre-signature, so no scalar "
            "can be recovered from it. That covers three situations and the response to all "
            "three is the same: treat the counterparty's signature as unrelated and do not "
            "proceed to reconstruct a spend key"
        )
    if public_key_for_share(recovered).hex() != expect_spend_public.lower():
        raise ProtocolError(
            "the scalar recovered from the broadcast signature is the discrete log of the "
            "secp256k1 adaptor point but NOT of the ed25519 spend share the lock address was "
            "built from. The cross-curve binding is absent or was never verified against these "
            "keys, and adding this scalar to the other share would produce a spend key that "
            "opens nothing"
        )
    return recovered


def reconstruct_spend_key(my_share: int, counterparty_share: int) -> int:
    """`s = s_a + s_b mod l`, the key that spends the locked XMR. Never printed, never stored.

    The addition happens HERE and only here, on the ed25519 side, after both shares are
    known as integers -- which is the rule that section 3.2 of the DLEQ design document
    exists to enforce. Measured in this container: 961 of 2000 random share pairs from
    `[1, 2^252)` sum to at least `l`, so for roughly half of all swaps the sum reduces on
    ed25519 while the corresponding secp256k1 sum does not, and the two commit to
    different integers with no error anywhere. Nothing may derive one from the other.

    Both shares are range-checked again on the way in. That is not paranoia about the
    caller: `counterparty_share` arrives from `recover_counterparty_spend_share`, which
    reads it out of a signature a counterparty broadcast, so it is externally influenced
    data reaching the most sensitive arithmetic in the tree. The range check is cheap and
    the failure it catches is a spend key that opens nothing.
    """
    return shared_private_spend_key(
        require_share_in_range(my_share, "my spend share"),
        require_share_in_range(counterparty_share, "counterparty spend share"),
    )


def xmr_lock_wait_seconds(confirmations: int) -> int:
    """How long, in seconds, before XMR sent to the lock is both confirmed and SPENDABLE.

    `max(confirmations, XMR_OUTPUT_LOCK_BLOCKS)` blocks and not the sum: the 10-block
    output lock and the confirmation wait run CONCURRENTLY -- both are counted in blocks
    from the same transaction -- so adding them would double-count and produce a timelock
    requirement twice as long as the chain imposes. Asking for fewer than 10
    confirmations does not make the output spendable sooner, which is the asymmetry the
    `max` expresses.

    Seconds, not µfn: this feeds arithmetic, and rule 6's boundary puts the conversion at
    the print rather than at the calculation.
    """
    if confirmations < 0:
        raise ProtocolError(f"confirmations must not be negative, got {confirmations}")
    return max(confirmations, XMR_OUTPUT_LOCK_BLOCKS) * XMR_SECONDS_PER_BLOCK


def s_chain_confirmation_seconds(asset: str, confirmations: int) -> int:
    """Seconds for `confirmations` blocks on the script chain, from the one shared table.

    `htlc_timelock.SECONDS_PER_BLOCK` and not a local copy, which is rule 11's shape: one
    vocabulary, derived in one place. An unknown asset raises rather than defaulting to
    any chain's interval -- a default would silently price a Bitcoin confirmation at
    Gridcoin's 90 seconds, which is the wrong direction by a factor of about seven and
    would make every margin below look satisfied when it is not.
    """
    if asset not in SECONDS_PER_BLOCK:
        raise ProtocolError(
            f"unknown script chain {asset!r}; this protocol's script leg is one of "
            f"{sorted(SECONDS_PER_BLOCK)}. Monero is the OTHER leg and has no timelock at all"
        )
    if confirmations < 0:
        raise ProtocolError(f"confirmations must not be negative, got {confirmations}")
    return confirmations * SECONDS_PER_BLOCK[asset]


def assert_timelock_ordering(plan: TimelockPlan) -> None:
    """REFUSE a timelock plan that lets either side be robbed. Hazards 1 and 2, in seconds.

    Two comparisons, both with a positive margin, both refusing rather than warning -- a
    warning on this is a warning nobody reads until a swap has been taken.

    HAZARD 2, the first comparison: there must be enough time before T1 to fund the Monero
    leg, wait for it to confirm AND unlock, and get the script-chain redeem confirmed. If
    T1 arrives first, the cancel path opens while the Monero is already locked, and the
    funder is left dependent on the counterparty refunding to get it back. They are never
    left with nothing -- the punish path is compensation -- but they can be left holding
    the wrong asset, which is not the trade they agreed to.

    HAZARD 1, the second comparison: `T2 - T1` must cover the cancel's own confirmations
    plus the longest the first funder may be unresponsive. From the moment the script leg
    is funded, the counterparty holds plain signatures on both cancel and punish and can
    take the coin by publishing them in sequence -- the only defense is that the funder
    publishes the refund in between, which requires being awake. That is not fixable and it
    is not a flaw: a punish path the first funder could disarm would leave the other side
    with locked XMR and no recourse. So the first funder carries a LIVENESS obligation, and
    this comparison is that obligation written as a number.

    THE UNIT IS SECONDS AND THIS IS WHERE THE PREVIOUS GENERATION OF THIS CHECK WENT
    WRONG ONE FILE OVER. `atomic_swap.py::assert_ordering` compared BLOCKS REMAINING
    across two chains and refused a correctly built BTC/LTC swap on the operator's first
    real run, 2026-09-27, because Litecoin needs four times the blocks for half the time.
    This protocol has THREE block intervals in play -- the script chain's, and Monero's
    120s, which is not a timelock at all and only converts the output lock into a wait --
    so the same error is available in more places. Everything here is seconds.

    Raises ProtocolError naming the shortfall in µfn with seconds in parentheses, because
    the reader of a refusal is an operator deciding what to change, not a machine.
    """
    if plan.margin_seconds <= 0:
        raise ProtocolError(
            f"margin_seconds must be positive, got {plan.margin_seconds}. A comparison with zero "
            f"margin is a comparison that one slow block breaks, and both checks below are the "
            f"kind where being exactly on the line means being robbed"
        )

    needed_before_cancel = (
        xmr_lock_wait_seconds(plan.xmr_confirmations)
        + s_chain_confirmation_seconds(plan.s_chain_asset, plan.redeem_confirmations)
        + plan.margin_seconds
    )
    if plan.seconds_until_cancel <= needed_before_cancel:
        shortfall = needed_before_cancel - plan.seconds_until_cancel
        raise ProtocolError(
            f"REFUSING: the cancel timelock T1 is {format_duration(plan.seconds_until_cancel)} "
            f"away but funding the Monero leg, waiting for it to confirm and unlock, and "
            f"confirming the script-chain redeem needs "
            f"{format_duration(needed_before_cancel)} -- short by "
            f"{format_duration(shortfall)}. Funding into that window leaves the Monero locked "
            f"with the cancel path already open. Nothing was funded"
        )

    punish_window = plan.seconds_until_punish - plan.seconds_until_cancel
    needed_after_cancel = (
        s_chain_confirmation_seconds(plan.s_chain_asset, plan.cancel_confirmations)
        + plan.offline_allowance_seconds
        + plan.margin_seconds
    )
    if punish_window <= needed_after_cancel:
        shortfall = needed_after_cancel - punish_window
        raise ProtocolError(
            f"REFUSING: the punish timelock T2 is only {format_duration(punish_window)} after the "
            f"cancel timelock T1, and confirming the cancel plus the allowance for the first "
            f"funder to be unresponsive needs {format_duration(needed_after_cancel)} -- short by "
            f"{format_duration(shortfall)}. In that window the first funder can lose its coin to "
            f"the punish path while doing nothing wrong. Nothing was funded"
        )


def redeem_is_safe_to_broadcast(
    *, seconds_until_cancel: int, s_chain_asset: str, redeem_confirmations: int, margin_seconds: int
) -> tuple[bool, str]:
    """May the redeem be broadcast now? HAZARD 3, and it is the one both-legs outcome.

    Stated plainly, because the brief asked for any step that lets one side take both legs
    to be said plainly rather than buried:

        The instant the redeem is broadcast, the spend share inside it is public. If that
        transaction is then re-orged out and the cancel confirms in its place, the
        counterparty holds BOTH spend shares AND its own script-chain coin. It sweeps the
        Monero and keeps the coin. IT HAS BOTH LEGS AND THE OTHER PARTY HAS NOTHING.

    Three things are true about that at once and all three have to be said together. It is
    not a flaw in the cryptography -- every signature involved is exactly what it claims to
    be. It is inherent to an adaptor swap and is not something this design introduced. And
    it is bounded entirely by ONE quantity: the confirmation depth the redeem reaches
    before T1. A redeem confirmed deeply before T1 cannot be replaced by a transaction that
    is not valid until T1.

    So this is a refusal rather than a warning, and the caller's correct response to False
    is to take the cancel path instead -- accepting the refund-or-punish outcome, which
    costs the trade but not the asset. A protocol that broadcasts the redeem "because we
    finally got the pre-signature" with an hour left before T1 is the version of this that
    loses money, and it is the version anybody writes first.

    Returns a verdict with a reason rather than raising, because unlike
    `assert_timelock_ordering` -- which refuses to set a swap up at all -- this one is
    consulted at a moment when there IS a correct alternative action, and the caller has
    to be able to take it without catching an exception to find out that it should.
    """
    needed = s_chain_confirmation_seconds(s_chain_asset, redeem_confirmations) + margin_seconds
    if margin_seconds <= 0:
        return False, (
            f"margin_seconds must be positive, got {margin_seconds}; a zero-margin race against a "
            f"timelock is the race this check exists to refuse"
        )
    if seconds_until_cancel <= needed:
        return False, (
            f"REFUSING to broadcast the redeem: the cancel timelock is "
            f"{format_duration(seconds_until_cancel)} away and confirming the redeem to "
            f"{redeem_confirmations} blocks on {s_chain_asset} needs "
            f"{format_duration(needed)}. Broadcasting publishes the spend share, and if the "
            f"redeem is then re-orged out and the cancel confirms instead, the counterparty "
            f"holds both shares AND its own coin -- both legs. Take the cancel path instead"
        )
    return True, "the redeem can confirm well before the cancel timelock opens"


def monero_network_is_test(nettype: str | None) -> tuple[bool, str]:
    """Is this monerod on a network where a mistake costs nothing? ASKED, not inferred.

    `nettype` comes from monerod's own `get_info` response. Three answers are acceptable --
    fakechain (regtest), stagenet, testnet -- and everything else is refused, INCLUDING a
    missing or unrecognized value. A daemon that will not say which chain it is on is a
    daemon this protocol will not fund a lock on, which is the same posture
    `atomic_swap.py::chain_name` takes for the script chain: refusing rather than assuming.

    Do NOT reach for `validate_address` to answer this. It reports the network of the
    ADDRESS FORMAT it was handed, not the network of the daemon, and on a regtest chain
    those disagree -- regtest addresses carry the mainnet prefix. It will confidently say
    "mainnet" about a perfectly good regtest address.
    """
    if not nettype:
        return False, (
            "monerod did not report a nettype, so the network cannot be established. Refusing "
            "rather than assuming: a daemon that will not say which chain it is on is not one to "
            "lock funds on"
        )
    normalized = str(nettype).strip().lower()
    if normalized in XMR_TEST_NETTYPES:
        return True, f"monerod reports nettype={normalized!r}, which is a test network"
    return False, (
        f"monerod reports nettype={normalized!r}, which is not one of "
        f"{sorted(XMR_TEST_NETTYPES)}. Refusing"
    )


def monero_address_network(nettype: str) -> str:
    """The address PREFIX network for a daemon nettype. Not the identity map.

    `fakechain -> mainnet` is the row that is not, and it is the one that matters: a regtest
    wallet's addresses carry the mainnet prefix byte 18, because
    `cryptonote_config.h:361` returns the mainnet config for FAKECHAIN and there is no
    regtest prefix at all. A shared address built with a "regtest" prefix would not exist,
    and one built with a testnet prefix would not be recognized by the wallet that has to
    sweep it.

    Raises on an unknown nettype rather than defaulting. A default of "mainnet" would be the
    dangerous direction (it would silently build a real-network address) and a default of
    "stagenet" would produce an address no daemon accepts, so neither is better than a
    refusal that names the value.
    """
    normalized = str(nettype).strip().lower()
    if normalized not in XMR_ADDRESS_NETWORK_BY_NETTYPE:
        raise ProtocolError(
            f"unknown Monero nettype {nettype!r}; expected one of "
            f"{sorted(XMR_ADDRESS_NETWORK_BY_NETTYPE)}. The address prefix cannot be chosen "
            f"without it, and guessing one produces an address that either does not exist or is "
            f"on the wrong network"
        )
    return XMR_ADDRESS_NETWORK_BY_NETTYPE[normalized]


def rehearse(helper, network: str, message_hash: bytes) -> RehearsalResult:
    """Run the whole cryptographic protocol OFFLINE, both sides in one process. No chain.

    This is the composition, and what it proves is worth stating precisely because it is
    easy to over-read. It establishes, with no daemon and no network, that:

      - two independently sampled shares each produce a cross-curve proof that the other
        party's verifier accepts against the exact keys it holds;
      - both parties compute the same lock address from the four public halves;
      - an adaptor pre-signature made under one party's adaptor point pre-verifies;
      - completing it with that party's spend share yields a signature from which the
        share is recovered EXACTLY, and the recovered share's ed25519 public key is the one
        the address was built from;
      - the reconstructed spend key's public key equals the lock's summed public spend key
        -- which is the property the regtest sweep demonstrated on a chain.

    WHAT IT DOES NOT PROVE, and the caveat is the point. BOTH SIDES ARE PLAYED BY ONE
    PROCESS, so nothing here tests that a counterparty who is NOT the prover reaches the
    same verdict, that the Monero lock can be funded, that it unlocks, that the
    reconstructed key imports into a wallet, or that any script-chain transaction exists
    to carry the pre-signature -- and that last one is absent from the tree entirely (see
    the module docstring's fifth component). `atomic_swap.py::report_completed_swap` names
    the same caveat about its own runs, for the same reason: one process playing two
    parties proves the cryptography and the arithmetic, and proves nothing about the
    transport.

    `message_hash` is the 32-byte digest of the script-chain redeem transaction the
    pre-signature is over. It is a PARAMETER rather than a constant because the digest is
    the thing a real swap binds to a specific transaction, and a rehearsal that invented
    its own would be exercising a different statement than the one the protocol makes.
    In this repo that digest comes from `htlc_spend.legacy_sighash`.

    No private value is returned. Both parties' shares exist only inside this function.
    """
    initiator_shares = sample_shares()
    participant_shares = sample_shares()

    initiator = commit_shares(helper, initiator_shares)
    participant = commit_shares(helper, participant_shares)

    # Each side verifies the OTHER's commitment, which is the direction that matters. A
    # party verifying its own proof learns nothing; `verify_share_commitment` raises, so
    # reaching the line after both calls is the verdict.
    verify_share_commitment(helper, participant)
    verify_share_commitment(helper, initiator)
    dleq_verified_both_ways = True

    address = lock_address(network, initiator, participant)
    if address != lock_address(network, participant, initiator):
        raise ProtocolError(
            "the two parties computed different lock addresses from the same four public shares. "
            "Point addition commutes, so this cannot happen for well-formed inputs, and an "
            "address the two sides disagree about is money sent where one of them cannot see it"
        )

    # The participant is the party receiving script-chain coin, so the redeem is pre-signed
    # under THEIR adaptor point and completing it publishes THEIR share. The initiator is
    # the script-chain funder and holds the signing key.
    signing_key = secrets.randbelow(adaptor_ecdsa.CURVE.order - 1) + 1
    adaptor_point = adaptor_ecdsa.point_from_bytes(bytes.fromhex(participant.adaptor_point))
    pre_signature = adaptor_ecdsa.pre_sign(signing_key, message_hash, adaptor_point)
    presignature_verified = adaptor_ecdsa.pre_verify(
        adaptor_ecdsa.public_key_point(signing_key), message_hash, adaptor_point, pre_signature
    )
    if not presignature_verified:
        raise ProtocolError(
            "the adaptor pre-signature did not verify against the key that made it and the "
            "adaptor point it was made under. Nothing downstream of this is meaningful"
        )

    signature = adaptor_ecdsa.adapt(pre_signature, participant_shares.spend)
    recovered = recover_counterparty_spend_share(
        pre_signature, signature, participant.spend_public
    )

    # Whether adapt()'s BIP62 low-S negation fired for this trial. Reported because it is
    # the branch of recover_adaptor_secret that fires on roughly half of real swaps, and a
    # rehearsal that never exercised it would be a rehearsal of the easy case. Measured
    # 6 of 12 across the trials taken on 2026-09-27.
    low_s_negation_exercised = (
        pre_signature.s_a * pow(signature[1], -1, adaptor_ecdsa.CURVE.order)
    ) % adaptor_ecdsa.CURVE.order != participant_shares.spend

    spend_key = reconstruct_spend_key(initiator_shares.spend, recovered)
    spend_key_opens_lock = scalar_base_mul(spend_key).compress() == shared_public_key(
        bytes.fromhex(initiator.spend_public), bytes.fromhex(participant.spend_public)
    )

    return RehearsalResult(
        lock_address=address,
        proof_bytes_initiator=len(initiator.dleq_proof),
        proof_bytes_participant=len(participant.dleq_proof),
        dleq_verified_both_ways=dleq_verified_both_ways,
        presignature_verified=presignature_verified,
        recovered_share_matches_commitment=recovered == participant_shares.spend,
        spend_key_opens_lock=spend_key_opens_lock,
        low_s_negation_exercised=low_s_negation_exercised,
    )


__all__ = [
    "ED25519_POINT_HEX_LEN",
    "SECP256K1_POINT_HEX_LEN",
    "SHARE_UPPER_BOUND",
    "XMR_ADDRESS_NETWORK_BY_NETTYPE",
    "XMR_OUTPUT_LOCK_BLOCKS",
    "XMR_SECONDS_PER_BLOCK",
    "XMR_TEST_NETTYPES",
    "PrivateShares",
    "ProtocolError",
    "RehearsalResult",
    "ShareCommitment",
    "ShareRangeError",
    "ShareRejected",
    "TimelockPlan",
    "assert_timelock_ordering",
    "commit_shares",
    "lock_address",
    "monero_address_network",
    "monero_network_is_test",
    "reconstruct_spend_key",
    "recover_counterparty_spend_share",
    "redeem_is_safe_to_broadcast",
    "redeem_presignature_may_be_released",
    "rehearse",
    "require_share_in_range",
    "s_chain_confirmation_seconds",
    "sample_shares",
    "verify_share_commitment",
    "xmr_lock_wait_seconds",
]
