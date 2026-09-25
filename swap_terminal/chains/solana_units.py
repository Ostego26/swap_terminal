"""Solana's vocabulary: amounts, commitment levels, rent and fees.

Role: function level (the bottom of CLAUDE.md rule 10's stack -- every name
      here is a pure function or a constant; nothing opens a socket)
Reads: nothing. No chain, no file, no environment.
Writes: nothing
Can move funds: no. It converts and classifies. It does decide what
      `min_confirmations` MEANS for Solana, which gates when a deposit is
      credited -- and CLAUDE.md rule 16 puts a confirmation threshold on the
      operator's side of the line, which is why the threshold itself is in
      config.Config and only its VOCABULARY is here.
Mainnet-safe: yes.

=============================================================================
CONFIRMATIONS ARE A CATEGORY MISMATCH, AND THIS IS WHERE IT IS RESOLVED
=============================================================================

Every other chain in this repository is proof-of-work. A deposit gets safer as
blocks pile on top of it, `min_confirmations` is a count of those blocks, and
`confirmations >= min_confirmations` in services/deposit_service.py is the gate
between "somebody sent us coins" and "we send coins back".

**Solana has no such count.** It has COMMITMENT LEVELS -- `processed`,
`confirmed`, `finalized` -- which describe how much of the validator set has
voted on the block a transaction landed in. Finality is by supermajority vote,
not by accumulated work.

The tempting move is to use Solana's own `confirmations` field, which
`getSignatureStatuses` returns. **That field is not what its name says**, and
using it would be exactly the unit laundering CLAUDE.md rule 6 refuses. It
counts blocks since the transaction was CONFIRMED, it is `null` once the
transaction is finalized, and it is not monotonic in safety: a `processed`
transaction sitting at depth 500 is LESS safe than a `finalized` one at depth
5, because the first can still be dropped by a fork and the second cannot.
Feeding that number into `>= min_confirmations` would produce a comparison that
looks exactly like Bitcoin's and means something else.

Slot depth -- `getSlot() - the transaction's slot` -- is the same trap wearing
a different name. It is a real, monotonic integer, which is what makes it
dangerous: it reads like a confirmation count and carries no safety claim at
all.

**SO THE INTEGER THIS MODULE PRODUCES IS A COMMITMENT RANK, AND IT IS NAMED
THAT EVERYWHERE.** The ladder is below, it has four rungs, and it is the ONE
place the vocabulary is derived (rule 11: one vocabulary, derived in one place,
applied identically). What `min_confirmations` means per chain is then:

    BTC / LTC / GRC     a count of blocks. 2, 2 and 6.
    SOL                 a rung on COMMITMENT_RANKS. 3 means finalized.

The identifier means the same thing in both cases -- "the integer a deposit's
own integer must reach before it is credited" -- which is what rule 11 actually
asks for. What it does NOT mean is that 3 SOL-rungs is comparable to 3 BTC
blocks, and nothing in this codebase ever compares them to each other.

**A MISCONFIGURED THRESHOLD IS A SILENT PERMANENT STALL**, which is why
validate_min_commitment_rank() exists and why it raises. An operator who copies
Bitcoin's habit and sets `SOL_MIN_CONFIRMATIONS=6` gets a number no deposit can
ever reach: every Solana deposit would sit at rank 3 forever, `confirmed_total`
would stay 0, and the swap would remain in `confirming` with nothing logged as
wrong. That is rule 14's worst shape -- "did nothing" wearing the same face as
"did work" -- so it is refused at construction time, loudly, with the ladder
printed in the message.

=============================================================================
RENT REPLACES DUST, AND IT DOES NOT BELONG IN htlc_fee.py
=============================================================================

modules/htlc_fee.py holds this repository's per-chain dust table, and the
question of whether Solana's rent-exempt minimum belongs beside it was asked
and answered NO. The reasons are structural rather than stylistic:

  it is a different suite.   htlc_fee.py belongs to the atomic-swap modules
      (modules/atomic_*_client.py, modules/htlc_spend.py). Nothing on the Flask
      app's path imports it, and Solana is being added to the Flask app.
  the function signature does not survive.   dust_threshold_satoshis(asset,
      script_pubkey) takes a Bitcoin scriptPubKey and computes
      (txout_bytes + spend_bytes) * relay_rate / 1000. Solana has no
      scriptPubKey, no txout, no per-kvB relay rate and no concept of an output
      being spent separately. Adding "SOL" to that table means a parameter that
      is meaningless for one of its assets, which is how a table stops being
      one rule and becomes two wearing one name (rule 8).
  they are not the same quantity.   A dust output is REFUSED BY RELAY POLICY:
      the transaction never propagates and nothing is lost. A Solana account
      below the rent-exempt minimum is ACCEPTED, exists, and is then collected
      by the runtime -- the funds go away after the fact. "Too small to send"
      and "small enough to evaporate" want different handling at the call site.

So the reasoning lives at both ends (rule 8: "the difference is the point and
belongs in a comment at BOTH sites, naming the other one"). modules/htlc_fee.py
carries a pointer back here.

=============================================================================
FEES ARE PER SIGNATURE, NOT PER BYTE
=============================================================================

htlc_fee.py's rule is `rate * size / 1000` -- a price per kilovirtualbyte,
because Bitcoin block space is the scarce thing. Solana's base fee is
5,000 lamports PER SIGNATURE and does not vary with transaction size at all. A
simple transfer has one signature and costs 5,000 lamports whether it moves one
lamport or a million SOL. (Priority fees are a separate, optional,
compute-unit-priced addition and are not modeled here: nothing in this
repository sets one.)

=============================================================================
WHAT IN HERE IS MEASURED AND WHAT IS A REFERENCE VALUE
=============================================================================

CLAUDE.md rule 17, and the distinction is load-bearing. **No Solana RPC
endpoint was reachable from the environment this was written in** --
api.devnet.solana.com and release.anza.xyz both returned 403 from the proxy --
so nothing below was confirmed against a running cluster.

  MEASURED HERE          the conversions, the ladder, and every function's
                         behavior, by tests/test_solana_units.py.
  REFERENCE VALUES       LAMPORTS_PER_SOL, SIGNATURE_FEE_LAMPORTS and the two
                         RENT_EXEMPT_* figures are Solana's published
                         constants, NOT observations. The adapter asks the
                         chain (getMinimumBalanceForRentExemption,
                         getFeeForMessage) rather than trusting them, and they
                         are here so a diagnostic can say what it EXPECTED
                         next to what the chain said.
"""

from __future__ import annotations

from decimal import Decimal

# --- amounts -----------------------------------------------------------------

# 1 SOL = 10^9 lamports. Native SOL's decimals; an SPL mint carries its own.
LAMPORTS_PER_SOL = 1_000_000_000
SOL_DECIMALS = 9

# The largest base-unit integer that survives a round trip through a Python
# float without losing a unit: 2^53. The Flask app's swap amounts are floats
# throughout (services/, db.py's REAL columns), so the boundary is real and is
# reported rather than hidden. For context, 2^53 lamports is about 9.0 million
# SOL -- far above any amount this terminal quotes -- but a token with 9
# decimals and a large supply can exceed it, which is why to_amount() says so
# instead of quietly rounding.
FLOAT_EXACT_INTEGER_LIMIT = 2**53


def base_units_to_amount(base_units: int, decimals: int) -> float:
    """Convert an integer base-unit amount to the float the app carries.

    THE ONE CONVERSION (CLAUDE.md rule 11: "is the conversion between a human
    amount and a base unit done once, in one function, rather than by
    multiplying by a literal at each call site?"). Every lamport-to-SOL and
    token-base-unit-to-token conversion in the Solana path goes through here
    and its inverse.

    Decimal rather than `base_units / 10**decimals` because the float division
    is wrong in the last place for ordinary values -- 123456789 lamports
    divided by 1e9 does not give the same digits as the exact quotient -- and
    the result is compared against a quoted amount inside a tolerance band.
    Decimal does the division exactly and float() rounds ONCE at the end,
    instead of accumulating.
    """
    if decimals < 0:
        raise ValueError(f"decimals cannot be negative, got {decimals}")
    return float(Decimal(int(base_units)) / (Decimal(10) ** decimals))


def amount_to_base_units(amount: float, decimals: int) -> int:
    """Convert a float amount to integer base units, rounding DOWN.

    Rounds down (truncates) rather than to-nearest, and the direction is
    deliberate on a payout path: rounding up would send a fraction of a unit
    more than was quoted, every time, out of the hot wallet. Truncation errs
    toward keeping it.

    Decimal(str(amount)) rather than Decimal(amount), because the second
    converts the float's exact binary value -- Decimal(0.1) is
    0.1000000000000000055511151231257827... -- and truncating THAT is a
    different answer from truncating the decimal number the operator typed.
    """
    if decimals < 0:
        raise ValueError(f"decimals cannot be negative, got {decimals}")
    return int(Decimal(str(amount)) * (Decimal(10) ** decimals))


def float_is_exact_for(base_units: int) -> bool:
    """True when this base-unit count survives the app's float columns intact.

    Exists so a diagnostic can WARN rather than a conversion silently rounding.
    Returning the fact is the honest half; what to do about it is the caller's.
    """
    return abs(int(base_units)) <= FLOAT_EXACT_INTEGER_LIMIT


# --- commitment --------------------------------------------------------------

# THE LADDER. Solana's three commitment levels plus the rung for "the cluster
# has no status for this signature", which is not a level Solana names but is a
# state a caller must be able to represent: a signature that has been dropped,
# or has not landed yet, is not `processed`.
#
# Rank 0 is deliberately the same integer a Bitcoin deposit has before it is
# mined, so that the "has it been seen at all" branches in
# services/deposit_service.py (`max_confirmations <= 0`) keep working without a
# per-chain special case.
COMMITMENT_RANKS: dict[str, int] = {
    "unknown": 0,
    "processed": 1,
    "confirmed": 2,
    "finalized": 3,
}

# The rung a Solana deposit must reach before this terminal credits it, and the
# default config.Config gives SOL_MIN_CONFIRMATIONS. Finalized, not confirmed,
# and the difference is money: a `confirmed` transaction has a supermajority
# vote but can still be rolled back by a sufficiently deep fork, while a
# `finalized` one is settled by definition. Waiting costs roughly 13 seconds
# (about 11µfn) more than `confirmed` on a healthy cluster. Releasing a payout
# against a deposit that then disappears costs the deposit.
FINALIZED_RANK = COMMITMENT_RANKS["finalized"]

# THE TWO COMMITMENTS THIS TERMINAL ASKS FOR, AND THEY ARE DIFFERENT ON PURPOSE.
#
# Discovery reads at `processed` -- the LOWEST level -- so that a deposit which
# has landed but is not yet settled is VISIBLE. That is what moves a swap to
# `deposit_seen` and then `confirming`, which is what a customer refreshing the
# page is looking at. Reading discovery at `finalized` would make a deposit
# invisible for its whole confirmation window and then appear already credited,
# which is CLAUDE.md rule 14's silence: the operator and the customer both lose
# the ability to tell "arriving" from "never sent".
#
# The GATE is a separate question and reads the rank, not this constant. Seeing
# a deposit at `processed` credits nothing; only rank >= SOL_MIN_CONFIRMATIONS
# does. So the low discovery commitment buys visibility and cannot buy an early
# payout.
DISCOVERY_COMMITMENT = "processed"

# What a balance read asks for. Finalized, because a hot-wallet balance is used
# to decide whether a payout can be covered, and an unsettled balance can go
# away. Reporting less than is there is the safe direction.
BALANCE_COMMITMENT = "finalized"


def commitment_rank(confirmation_status: str | None) -> int:
    """Map a Solana commitment level onto the ladder. Unknown maps to 0.

    THIS IS THE DECISION (rule 10) and it is a pure function of one string, so
    a test can seed every rung without a cluster.

    An unrecognized string maps to 0 rather than raising, and that is the safe
    direction on purpose: 0 means "not creditable", so a level this code has
    never heard of -- a future Solana release adding a fourth -- STALLS a swap
    rather than releasing it. Stalling is visible and recoverable; releasing is
    on-chain and final.
    """
    if confirmation_status is None:
        return COMMITMENT_RANKS["unknown"]
    return COMMITMENT_RANKS.get(str(confirmation_status).strip().lower(), COMMITMENT_RANKS["unknown"])


def rank_name(rank: int) -> str:
    """The level a rank stands for, for an operator to read (rule 14)."""
    for name, value in COMMITMENT_RANKS.items():
        if value == rank:
            return name
    return f"out-of-range({rank})"


def describe_commitment(rank: int, minimum: int, slot_depth: int | None = None) -> str:
    """One self-describing line: the rank, what it means, and whether it passes.

    CLAUDE.md rule 14: "state what the number means, next to the number. The
    operator reads the screen, not the source." A bare `confirmations=2` in a
    Solana log is actively misleading to anyone who has been reading Bitcoin
    logs all morning.

    The slot depth rides along as a DIAGNOSTIC and is labeled as one. It tells
    an operator how long ago the transaction landed; it is explicitly not what
    the gate reads, for the reason in the module docstring.
    """
    verdict = "CREDITABLE" if rank >= minimum else "not yet creditable"
    depth = f", slot depth {slot_depth} (diagnostic only -- NOT the gate)" if slot_depth is not None else ""
    return (
        f"commitment rank {rank} ({rank_name(rank)}) vs required {minimum} ({rank_name(minimum)}): "
        f"{verdict}{depth}  <- a rung on Solana's commitment ladder, NOT a count of blocks"
    )


def validate_min_commitment_rank(minimum: int) -> int:
    """Return `minimum` if it is a rung on the ladder; raise otherwise.

    WHY THIS REFUSES INSTEAD OF CLAMPING. An operator who sets
    SOL_MIN_CONFIRMATIONS=6 -- Gridcoin's value, and an entirely reasonable
    thing to type -- has asked for a rung that does not exist. Clamping it to 3
    would silently give them a threshold they did not choose, on the money
    path. Accepting it would make every Solana deposit uncreditable forever,
    with no error anywhere: the swap sits in `confirming`, the payout never
    fires, and the only symptom is a customer asking where their coins are.

    Rule 14's "make 'did nothing' look different from 'did work'" applied one
    level earlier: this configuration can only do nothing, so it is refused at
    the moment it is read rather than obeyed silently forever.
    """
    ladder = ", ".join(f"{value}={name}" for name, value in COMMITMENT_RANKS.items())
    if not isinstance(minimum, int) or isinstance(minimum, bool):
        raise ValueError(f"SOL_MIN_CONFIRMATIONS must be an integer rung on the commitment ladder ({ladder}), got {minimum!r}")
    if minimum not in COMMITMENT_RANKS.values():
        raise ValueError(
            f"SOL_MIN_CONFIRMATIONS={minimum} is not a rung on Solana's commitment ladder ({ladder}). "
            "Solana has commitment LEVELS, not a count of blocks -- a value above 3 can never be reached "
            f"by any deposit, and would stall every SOL swap silently. {FINALIZED_RANK} (finalized) is the default."
        )
    return minimum


# --- rent --------------------------------------------------------------------

# REFERENCE VALUES, NOT MEASUREMENTS. See the docstring's last section: no
# cluster was reachable from here. SolanaAdapter.rent_exempt_minimum() asks
# getMinimumBalanceForRentExemption, and these are what a diagnostic prints as
# "expected" beside the chain's answer.
#
# Solana charges rent for account storage and an account holding at least the
# rent-exempt minimum for its size is exempt forever. An account that falls
# BELOW it is collected by the runtime and its lamports are gone -- which is
# the structural difference from dust, spelled out in the docstring.
SYSTEM_ACCOUNT_SPACE = 0
TOKEN_ACCOUNT_SPACE = 165
RENT_EXEMPT_SYSTEM_ACCOUNT_LAMPORTS = 890_880
RENT_EXEMPT_TOKEN_ACCOUNT_LAMPORTS = 2_039_280


# --- fees --------------------------------------------------------------------

# Per SIGNATURE, not per byte. A one-signature transfer costs this regardless of
# its size. Reference value; getFeeForMessage is the authority.
SIGNATURE_FEE_LAMPORTS = 5_000


def transfer_fee_lamports(signature_count: int = 1) -> int:
    """The base fee for a transaction with this many signatures.

    Kept as a function rather than inlining the multiplication at call sites
    (rule 11's last test), and kept separate from any priority fee, which this
    repository does not set. A transfer whose destination needs an Associated
    Token Account created pays this PLUS the account's rent-exempt minimum, and
    that second part is not a fee -- it is a deposit into the new account, paid
    by whoever signs. Which is a fund decision and belongs to the operator.
    """
    if signature_count < 1:
        raise ValueError(f"a transaction has at least one signature, got {signature_count}")
    return SIGNATURE_FEE_LAMPORTS * signature_count
