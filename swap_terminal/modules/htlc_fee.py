#!/usr/bin/env python3
"""How much miner fee an HTLC redeem pays, derived from the transaction's size.

Role: function level (the decision -- one number, computed from one size)
Reads: the environment variables SWAP_REDEEM_FEE_COIN_PER_KVB_BTC / _LTC / _GRC
       when they are set, and nothing else. No chain, no file, no wallet.
Writes: nothing.
Can move funds: no directly -- it returns a Decimal. But the number it returns
       is subtracted from what the redeemer receives and handed to a miner, so
       every coin it decides is spent. Treat a change here as fund movement.
Mainnet-safe: yes in the sense that it opens nothing. The VALUES below are
       mainnet-shaped on purpose -- see "WHAT THIS COSTS" -- because the same
       constants are used on regtest and on a real chain, and a rule tuned to
       regtest is a rule that has never been tested where it matters.

WHY THIS FILE EXISTS, AND WHY IT IS NOT THE REASON I WAS GIVEN.

All three clients hardcoded a flat miner fee -- `Decimal("0.0001")` on BTC and
LTC, `Decimal("0.01")` on GRC -- and paid it regardless of how large the
transaction was. The brief that asked for this work predicted that fixing the
signing defect would simply move the failure one step later, to
`sendrawtransaction` refusing the spend as `absurdly-high-fee`:

    "On a ~250-byte redeem that is roughly 0.4 coin/kvB, about four times
     Core's default maxfeerate ceiling"

THAT IS WRONG BY EXACTLY A FACTOR OF A THOUSAND, and it is written down here
rather than quietly fixed because the same figure appears in this repository's
own regtest harness (regtest/steps.py's NO_FEE_LIMIT comment and its fee-policy
explanation, both corrected in the same commit) and would otherwise keep being
copied forward. Computed:

    0.0001 coin over 250 bytes = 0.0001 / 0.250 kvB = 0.0004 coin/kvB
    0.0001 coin over 323 bytes = 0.0001 / 0.323 kvB = 0.00030960 coin/kvB
    sendrawtransaction's default maxfeerate                 0.10 coin/kvB

So the flat fee sits 250 to 357 times BELOW the ceiling, not four times above
it. Reaching 0.4 coin/kvB on a 250-byte transaction would take a fee of 0.1
coin -- a thousand times the fee actually paid. A node would have accepted
every one of these broadcasts. Rule 17: the difference between a measurement
and a plausible reading is the whole point, and this one is arithmetic that
anybody can redo, which is why the numbers are here rather than the conclusion.

WHAT IS ACTUALLY WRONG WITH A FLAT FEE, then, and it is enough to fix:

  It is not a fee RATE. A fee is paid for the space a transaction occupies, and
  a constant is the correct amount at exactly one size. At 323 bytes the flat
  0.0001 works out to 0.00031 coin/kvB; at 2,000 bytes the same constant is
  0.00005 coin/kvB, a sixth of that, drifting toward the 0.00001 coin/kvB
  minimum relay fee and then under it. A redeem that fails to relay because it
  paid a flat fee is an HTLC that expires and a swap that is lost.

  It cannot be raised without a deploy. An HTLC redeem is time-critical in a
  way an ordinary send is not: if it does not confirm before the locktime, the
  counterparty refunds and takes both legs. Underpaying does not delay the
  swap, it loses it, and a constant in three source files is not something an
  operator can change while a contract is open.

THE RULE, STATED ONCE, IN ONE PLACE (rule 11: one vocabulary, derived here):

    fee = max(rate * size_in_bytes / 1000, floor)   rounded UP to 8 decimals

`rate` is a coin-per-kvB figure per chain and `floor` is an absolute minimum
per chain. Two numbers rather than one because they guard opposite ends: the
rate keeps a LARGE transaction paying enough to be relayed, and the floor keeps
a SMALL one above the minimum relay fee, which is itself a rate and so cannot
be cleared by a rate alone on a short transaction.

WHAT THIS COSTS, WHICH IS THE LINE THE OPERATOR READS.

EVERY FLOOR BELOW IS THE FLAT FEE THAT CHAIN ALREADY PAID. That is the whole
design of the numbers: at an ordinary redeem size the floor binds, so the fee
does not move at all, and the rate only takes over on a transaction large
enough that the flat fee would have been underpriced.

  chain  typical redeem     old flat fee   new fee        which term binds
  BTC    323 B, 1 output    0.0001         0.0001         the floor -- NO CHANGE
  LTC    357 B, 2 outputs   0.0001         0.00010710     the rate, by 7.1%
  GRC    360 B, 2 outputs   0.01           0.01           the floor -- NO CHANGE

  the crossover on BTC and LTC is 334 bytes: below it the floor binds and the
  fee is exactly what it was, above it the fee grows with the transaction.
  A 1,000-byte spend pays 0.0003 rather than 0.0001, which is the case the
  flat fee got wrong.

So this changes what a redeem pays on ONE of the three chains, by seven
thousandths of a percent of a typical contract, and leaves the other two
paying exactly what they paid. That is deliberate: the measurement above says
the flat fee was not causing a failure, and a fund-path change nobody needs
should be as small as the correctness argument requires and no larger.

THE BAND THE RATE SITS IN, so a future reader can move it knowingly:

    minimum relay fee   0.00001 coin/kvB on Bitcoin Core's default. Litecoin's
                        and Gridcoin's defaults are higher and are NOT verified
                        here, which is why this does not sit near the bottom.
    this rule           0.0003 coin/kvB on BTC and LTC -- 30x over Bitcoin's
                        relay minimum and 333x under the refusal ceiling.
    refusal ceiling     0.10 coin/kvB, sendrawtransaction's default maxfeerate.

`assert_within_broadcast_ceiling()` proves the upper end for the ACTUAL
transaction rather than trusting the arithmetic above -- which, given what the
first paragraph of this docstring is about, is the habit worth keeping.

WHAT THIS RULE IS NOT. It is not a fee ESTIMATOR. It does not call
`estimatesmartfee` and does not look at mempool congestion, so on a busy
mainnet it can be too low exactly when being too low is most expensive. That is
a deliberate limit: an RPC that returns a different number every minute cannot
be unit-tested without a chain, and this repository's rule is that a decision
belongs somewhere it can be called with seeded inputs. The escape hatch for
congestion is the environment override below, which an operator can raise
without a deploy.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from decimal import ROUND_UP, Decimal, InvalidOperation

# varint_length is imported rather than re-implemented (rule 8). It is three
# lines, and spelling it again here is exactly how one encoding rule becomes
# two that agree on the day they are written. modules/htlc_spend does not
# import this file, so there is no cycle; modules/htlc_rpc already imports
# both, so nothing gains a dependency it did not already have.
from modules.htlc_spend import varint_length

# Coin per kvB, per chain. Chosen so that the FLOOR below -- which is each
# chain's old flat fee -- is what binds at an ordinary redeem size, and the
# rate only takes over above 334 bytes. See the module docstring for the band
# this sits in and for what it costs.
FEE_RATE_COIN_PER_KVB: dict[str, Decimal] = {
    "BTC": Decimal("0.0003"),
    "LTC": Decimal("0.0003"),
    # Gridcoin's rate is twenty times the others because GRC's own minimum
    # relay fee is higher and because its unit is worth very much less. At an
    # ordinary redeem size the FLOOR below is what actually applies.
    "GRC": Decimal("0.01"),
}

# The absolute minimum fee, per chain. Each is the flat fee that chain's client
# used to hardcode, kept as the floor so that no redeem on any chain pays LESS
# than this repository already intended to pay.
MINIMUM_FEE_COIN: dict[str, Decimal] = {
    "BTC": Decimal("0.0001"),
    "LTC": Decimal("0.0001"),
    "GRC": Decimal("0.01"),
}

# --------------------------------------------------------------------------
# DUST: the OTHER end of the amount a redeem may pay, and the one that was
# missing entirely until 2026-09-25
# --------------------------------------------------------------------------
#
# WHAT WAS WRONG. modules/htlc_rpc._unsigned_transaction()'s only amount guard
# was `destination_amount <= 0`. Nothing checked whether an output was large
# enough for a node to RELAY, and the LTC and GRC platform fee is a fixed
# 0.25% of the contract -- so it shrinks with the contract while a dust limit
# does not.
#
# MEASURED 2026-09-25 through the real LTCClient.redeem_contract(): a 0.01 LTC
# contract builds outputs of [986880, 2500] satoshis. The 2500 is the platform
# fee, and PLATFORM_FEE_LTC_ADDRESS's shipped default is a bech32 `tltc1q...`
# -- P2WPKH, whose dust limit on Litecoin Core 0.21.4 is 2,940 satoshis. So the
# whole transaction would be refused with code=-26 dust, and NO CLIENT HERE
# IMPLEMENTS A REFUND: a redeemer whose broadcast is refused has no recovery
# path and loses their own already-funded leg. The harness could not see it --
# CONTRACT_AMOUNT is "1.0", which puts the platform fee 85x over the limit, and
# regtest does not enforce standardness anyway. BTC has the same hole with no
# platform fee at all: a contract under 0.00010546 leaves a sub-546 destination.
#
# The destination figure is 986,880 and not 986,790, which is the number the
# review that found this reported, and the 90 satoshis are worth a sentence
# because they are the whole reason this table is per-SCRIPT. 986,790 is a
# 357-byte transaction and 986,880 is a 354-byte one: paying the platform fee
# to a P2PKH address makes the output 34 bytes, and paying it to the bech32
# address actually shipped in PLATFORM_FEE_LTC_ADDRESS makes it 31. The
# smaller output is also the one with the LOWER threshold -- 2,940 rather than
# 5,460 -- so the shipped default is simultaneously the cheaper transaction
# and the harder case to catch. Both are refused. The two numbers that decide,
# 2,500 and 2,940, are identical either way.
#
# THE RULE IS BITCOIN CORE'S OWN, and it is derived per OUTPUT rather than
# taken as one constant, because the threshold depends on what it would cost
# to spend the output it is protecting:
#
#     dust = (serialized size of the txout + size of the input that spends it)
#            x the chain's dust relay rate / 1000
#
#     txout      8 bytes of value + the compact size + the scriptPubKey
#                P2SH 32, P2PKH 34, P2WPKH 31
#     spend      148 for a legacy input; 67 for a witness one, which is Core's
#                32+4+1+(107/4)+4 with the witness discount applied
#
# An output is dust when its value is strictly LESS than that, which is Core's
# `nValue < GetDustThreshold(...)` -- so a P2PKH output of exactly 546 on BTC
# is fine and 545 is not.
# SOLANA IS NOT IN THIS TABLE, AND THAT WAS A DECISION RATHER THAN AN OMISSION.
# Recorded here as well as at the other end, because CLAUDE.md rule 8 asks that
# where two implementations genuinely differ "the difference is the point and
# belongs in a comment at BOTH sites, naming the other one." The other one is
# chains/solana_units.py, whose RENT_EXEMPT_* constants are Solana's analogue
# of this table, and its module docstring carries the full argument. The short
# form, three reasons:
#
#   different suite       this file belongs to the atomic-swap modules; Solana
#                         was added to the Flask app, which imports none of it.
#   the signature dies    dust_threshold_satoshis(asset, script_pubkey) takes a
#                         Bitcoin scriptPubKey and prices (txout_bytes +
#                         spend_bytes) at a per-kvB relay rate. Solana has no
#                         scriptPubKey, no txout and no per-byte pricing -- its
#                         fee is 5,000 lamports per SIGNATURE.
#   not the same quantity dust is REFUSED BY RELAY: the transaction never
#                         propagates and nothing is lost. A Solana account
#                         below the rent-exempt minimum is ACCEPTED, exists,
#                         and is then collected by the runtime -- the funds go
#                         away after the fact.
#
# Adding "SOL" here would mean a parameter meaningless for one of its assets,
# which is how one table quietly becomes two rules sharing a name.
DUST_RELAY_FEE_SAT_PER_KVB: dict[str, int] = {
    # Bitcoin Core's DUST_RELAY_TX_FEE. Gives 546 for P2PKH, 294 for P2WPKH.
    "BTC": 3000,
    # Litecoin Core 0.21.4's is TEN TIMES Bitcoin's, which is the fact that
    # makes this a live defect rather than a theoretical one: 5,460 for P2PKH
    # and 2,940 for P2WPKH.
    "LTC": 30000,
    # GRIDCOIN HAS NO DUST RULE, and this is READ FROM THE POLICY SOURCE rather
    # than assumed from Bitcoin's (rule 17). gridcoin-community/
    # Gridcoin-Research, master branch, read 2026-09-25:
    #
    #   src/policy/policy.cpp   IsStandardTx() walks tx.vout and the only
    #                           amount test in it is `if (txout.nValue == 0)
    #                           return false;`. There is no dust branch.
    #   whole repository        zero occurrences of IsDust, GetDustThreshold or
    #                           DUST_RELAY_TX_FEE -- each searched separately
    #                           across the repository, not just these files.
    #   src/main.cpp:117        nMinimumInputValue = 0.
    #   src/consensus/consensus.h:26-28
    #                           the only amount floor is a FEE floor --
    #                           MIN_TX_FEE = MIN_RELAY_TX_FEE = 10000 halfords,
    #                           scaled by (1 + bytes/1000). A fee rule, not an
    #                           output rule.
    #
    # So a rate of 0, and MINIMUM_OUTPUT_SATOSHIS below is what actually binds
    # on GRC -- which is exactly Gridcoin's rule, expressed in the same
    # machinery as the other two rather than as a special case.
    #
    # WHAT THIS IS NOT: a measurement against a running Gridcoin daemon. There
    # is none in this setup. It is a reading of the current source of the
    # reference implementation, which is a weaker claim than the BTC and LTC
    # rows -- those were measured against daemons -- and an operator on an
    # older build should check their own before trusting it.
    "GRC": 0,
}

# Every chain here refuses an output worth nothing. On BTC and LTC the dust
# threshold is far above it and this never binds; on GRC it is the whole rule.
# It also catches a 0.25% platform fee that quantizes to zero, which on GRC --
# with no dust threshold above it -- is otherwise the one way to build a
# non-standard transaction that the old `<= 0` guard could never have seen,
# because that guard only ever looked at the DESTINATION.
MINIMUM_OUTPUT_SATOSHIS = 1

# Core's GetDustThreshold: the cost of spending the output being protected.
# 148 = 32 (txid) + 4 (vout) + 1 (script length) + 107 (scriptSig) + 4
# (sequence). The witness figure is the same sum with 107 scaled down by
# WITNESS_SCALE_FACTOR 4 and integer-divided: 32 + 4 + 1 + 26 + 4.
LEGACY_INPUT_SPEND_BYTES = 148
WITNESS_INPUT_SPEND_BYTES = 67

# Eight bytes of value precede every output's script.
TXOUT_VALUE_BYTES = 8

# BIP141: a witness program is OP_0..OP_16 followed by a single push of 2 to 40
# bytes, which is what Core's CScript::IsWitnessProgram() checks.
WITNESS_PROGRAM_MIN_PUSH = 2
WITNESS_PROGRAM_MAX_PUSH = 40
OP_0 = 0x00
OP_1 = 0x51
OP_16 = 0x60

# The same 1000 the fee rate uses, as an int, because the dust arithmetic is
# integer satoshis throughout and Core's is too -- its CFeeRate::GetFee
# truncates, so doing this in Decimal and rounding would give a threshold this
# repository believes and no node applies.
BYTES_PER_KVB_INT = 1000


def is_witness_program(script_pubkey: bytes) -> bool:
    """BIP141's shape test: a version opcode, then one push of 2 to 40 bytes.

    Read off the BYTES, never off a rendered address or a daemon's
    `scriptPubKey.type` string, for the same reason every other comparison in
    this package is: Bitcoin Core 28.1 and Litecoin Core 0.21.4 disagree about
    what a decoded scriptPubKey contains, and agree about the hex.
    """
    if len(script_pubkey) < WITNESS_PROGRAM_MIN_PUSH + 2 or len(script_pubkey) > WITNESS_PROGRAM_MAX_PUSH + 2:
        return False
    version = script_pubkey[0]
    if version != OP_0 and not (OP_1 <= version <= OP_16):
        return False
    return script_pubkey[1] == len(script_pubkey) - 2


def dust_threshold_satoshis(asset: str, script_pubkey: bytes) -> int:
    """The smallest value this chain will relay in an output paying `script_pubkey`.

    Args:
        asset: BTC, LTC or GRC. It selects the dust relay rate and nothing else.
        script_pubkey: the output's script, as the node laid it out. The bytes,
            never an address -- what lets one call answer for a P2WPKH
            platform-fee address and a P2PKH destination in the same
            transaction is that it reads the shape instead of being told it.

    Raises:
        ValueError: if the asset is unknown. Adding a chain means adding its
            dust relay rate here, in this one table, beside its fee rate and
            its floor (rule 11).
    """
    if asset not in DUST_RELAY_FEE_SAT_PER_KVB:
        raise ValueError(
            f"no dust rule for asset {asset!r}; known assets are {', '.join(SUPPORTED_ASSETS)}. "
            "Adding a chain means adding its dust relay rate here, beside its fee rate and its floor."
        )
    spend_bytes = WITNESS_INPUT_SPEND_BYTES if is_witness_program(script_pubkey) else LEGACY_INPUT_SPEND_BYTES
    txout_bytes = TXOUT_VALUE_BYTES + varint_length(len(script_pubkey)) + len(script_pubkey)
    by_rate = (txout_bytes + spend_bytes) * DUST_RELAY_FEE_SAT_PER_KVB[asset] // BYTES_PER_KVB_INT
    return max(by_rate, MINIMUM_OUTPUT_SATOSHIS)


def describe_dust_threshold(asset: str, script_pubkey: bytes) -> str:
    """One self-describing line: the threshold, and every number that decided it.

    Rule 14 -- an operator reading a refusal must not have to open this file to
    learn why 2,500 satoshis was too small.
    """
    spend_bytes = WITNESS_INPUT_SPEND_BYTES if is_witness_program(script_pubkey) else LEGACY_INPUT_SPEND_BYTES
    txout_bytes = TXOUT_VALUE_BYTES + varint_length(len(script_pubkey)) + len(script_pubkey)
    kind = "witness" if is_witness_program(script_pubkey) else "legacy"
    return (
        f"{asset} dust threshold is {dust_threshold_satoshis(asset, script_pubkey)} satoshis "
        f"= ({txout_bytes}-byte output + {spend_bytes}-byte {kind} spend) x "
        f"{DUST_RELAY_FEE_SAT_PER_KVB[asset]} sat/kvB / 1000, floored at {MINIMUM_OUTPUT_SATOSHIS}"
    )


def assert_no_output_is_dust(asset: str, outputs: Sequence[tuple[int, bytes]]) -> None:
    """Refuse a spend that carries an output no node will relay. BEFORE SIGNING.

    Args:
        asset: BTC, LTC or GRC.
        outputs: every output as (value in satoshis, scriptPubKey bytes) --
            exactly modules/htlc_spend.ParsedTransaction.outputs, which is the
            node's OWN layout read back, not this repository's arithmetic about
            what it asked for.

    Raises:
        ValueError: naming which output, its value, the threshold, and the
            arithmetic that produced the threshold. The same shape as the
            `nothing would be left to send` refusal it sits beside, and for the
            same reason: a number in a refusal that the operator has to come
            back here to interpret is rule 14's defect.

    IT REFUSES; IT DOES NOT REALLOCATE. Dropping the platform-fee output and
    paying the remainder to the destination would make every one of these
    transactions broadcastable, and it is a decision about where somebody
    else's money goes. That is fund movement and it is the operator's
    (rule 16), so the refusal says so rather than quietly choosing.

    WHY IT IS CHECKED HERE AND NOT LEFT TO THE NODE. The node's refusal is
    `code=-26 dust` arriving after the spend is signed and submitted, and on a
    chain whose sendrawtransaction applies no such policy at all (Gridcoin's
    IsStandardTx has no dust branch -- see the table above) no refusal arrives.
    More to the point: NO CLIENT IN THIS PACKAGE IMPLEMENTS A REFUND. A
    redeemer whose broadcast is refused cannot recover their own funded leg, so
    the refusal has to arrive while a different decision is still possible.
    """
    for index, (satoshis, script_pubkey) in enumerate(outputs):
        threshold = dust_threshold_satoshis(asset, script_pubkey)
        if satoshis >= threshold:
            continue
        raise ValueError(
            f"{asset}: output {index} pays {satoshis} satoshis to scriptPubKey {script_pubkey.hex()}, "
            f"which is DUST -- {describe_dust_threshold(asset, script_pubkey)}. The node would refuse the "
            f"whole transaction with `code=-26 dust`, and no client here implements a refund, so a redeemer "
            f"whose broadcast is refused has no recovery path. Nothing was signed. Dropping an output or "
            f"paying its amount somewhere else is fund movement and is the operator's call, not this "
            f"function's."
        )


# --------------------------------------------------------------------------
# the PLATFORM fee, which is not the miner fee and was spelled twice
# --------------------------------------------------------------------------
#
# ONE RULE, TWO SPELLINGS, TWO FILES until 2026-09-25:
#
#     atomic_ltc_client.py   (Decimal("0.25") / Decimal(100)) * found.value
#     atomic_grc_client.py   Decimal("0.0025") * found.value
#
# They agree -- Decimal("0.25") / Decimal(100) is exactly Decimal("0.0025") --
# which is rule 8's whole point: two copies of one rule agree on the day they
# are written and drift from then on, invisibly, because each reads correctly
# in its own file. The GRC one had ALREADY drifted once in its comment, which
# said "2.5% of the total amount" beside an expression computing 0.25%, and
# that was caught only because somebody read the two side by side.
#
# It lives beside the miner fee because both are amounts subtracted from what
# the redeemer receives, and an operator asking "where does the money go"
# should find the answer once.
PLATFORM_FEE_RATE: dict[str, Decimal] = {
    "LTC": Decimal("0.0025"),
    "GRC": Decimal("0.0025"),
}

# BTC IS DELIBERATELY ABSENT rather than present as zero. BTCClient.
# redeem_contract() passes no extra outputs at all, and whether it should
# charge a platform fee is fund movement and the operator's (rule 16) -- it is
# the one row of the divergence table this merge did not settle. A zero entry
# here would read as "the table decided BTC charges nothing", which is a
# different and untrue statement: the table was never asked.


def platform_fee_coin(asset: str, contract_value: Decimal) -> Decimal:
    """The platform fee this chain charges on a redeem, quantized to the satoshi.

    Taken off the TOTAL, so it does not move when the miner fee does -- which
    is what both clients did and is the behavior this merge preserves exactly.
    Default (half-even) rounding, for the same reason: changing it would change
    what somebody is paid, and nothing here is asking to.

    Raises:
        ValueError: for an asset that charges none, naming BTC specifically,
            because "BTC charges nothing" and "BTC is missing from the table"
            are different sentences and a caller must not conflate them.
    """
    if asset not in PLATFORM_FEE_RATE:
        raise ValueError(
            f"no platform fee rule for asset {asset!r}; it is charged on {', '.join(PLATFORM_FEE_RATE)}. "
            "BTC is absent on purpose: BTCClient.redeem_contract() passes no extra outputs, and whether it "
            "should is the operator's call rather than this table's."
        )
    return (PLATFORM_FEE_RATE[asset] * contract_value).quantize(SATOSHI)


# sendrawtransaction's default maxfeerate, in coin per kvB, on Bitcoin Core and
# Litecoin Core. A transaction whose fee rate exceeds this is refused as
# `absurdly-high-fee` before it reaches the mempool. The flat fee this replaces
# was 250 to 357 times UNDER it, not over -- see the module docstring, which
# shows the arithmetic, because the opposite claim is written into this
# repository in three other places and was the stated reason for this file.
BROADCAST_CEILING_COIN_PER_KVB = Decimal("0.10")

# Eight decimal places on all three chains. Spelled once here rather than at
# each quantize() call.
SATOSHI = Decimal("0.00000001")

BYTES_PER_KVB = Decimal(1000)

SUPPORTED_ASSETS = tuple(FEE_RATE_COIN_PER_KVB)


def _override_env_name(asset: str) -> str:
    """The environment variable that replaces one chain's rate.

    ASCII, and the name carries its unit, which is rule 6's boundary: a µ in a
    name an operator has to export buys nothing and costs a support call. The
    unit is coin per kvB because that is what `maxfeerate` and `minrelaytxfee`
    are both expressed in, so an operator raising this can compare it directly
    against the two numbers the node will judge it by.
    """
    return f"SWAP_REDEEM_FEE_COIN_PER_KVB_{asset}"


def fee_rate_coin_per_kvb(asset: str) -> Decimal:
    """This chain's fee rate, after any environment override.

    Read at CALL time, never at import time. A module that reads os.environ
    when it is imported makes every later import order-dependent (rule 12), and
    it also means an operator who exports a new rate has to restart something
    to be sure it took.

    A malformed or out-of-band override RAISES rather than falling back to the
    default. An operator who sets this is doing it because a swap is at risk;
    silently ignoring what they typed and paying the old rate is the failure
    mode that matters here, and it would be invisible.
    """
    if asset not in FEE_RATE_COIN_PER_KVB:
        raise ValueError(
            f"no fee rule for asset {asset!r}; known assets are {', '.join(SUPPORTED_ASSETS)}. "
            "Adding a chain means adding its rate AND its floor here, in this one table (rule 11)."
        )
    raw = os.environ.get(_override_env_name(asset), "").strip()
    if not raw:
        return FEE_RATE_COIN_PER_KVB[asset]
    try:
        rate = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(
            f"{_override_env_name(asset)}={raw!r} is not a decimal number. It is a fee rate in coin per kvB, "
            f"for example {FEE_RATE_COIN_PER_KVB[asset]}."
        ) from exc
    if rate <= 0:
        raise ValueError(
            f"{_override_env_name(asset)}={raw!r} is not positive. A zero or negative fee rate would build a "
            "transaction no node will relay."
        )
    if rate > BROADCAST_CEILING_COIN_PER_KVB:
        raise ValueError(
            f"{_override_env_name(asset)}={raw!r} is above sendrawtransaction's default maxfeerate of "
            f"{BROADCAST_CEILING_COIN_PER_KVB} coin/kvB, so every spend built with it would be refused as "
            "absurdly-high-fee. That is the exact defect this fee rule exists to fix."
        )
    return rate


def minimum_fee_coin(asset: str) -> Decimal:
    """The absolute floor for this chain, which is the flat fee it used to pay."""
    if asset not in MINIMUM_FEE_COIN:
        raise ValueError(
            f"no fee floor for asset {asset!r}; known assets are {', '.join(SUPPORTED_ASSETS)}."
        )
    return MINIMUM_FEE_COIN[asset]


def redeem_miner_fee(asset: str, size_bytes: int) -> Decimal:
    """The miner fee for a spend of `size_bytes`, on `asset`.

    Rounded UP to the satoshi. Rounding down would produce a fee rate fractionally
    below the rate asked for, which on a chain whose minimum relay fee is exactly
    the configured rate is the difference between relayed and refused -- and the
    amount at stake in the rounding is one satoshi.

    Args:
        asset: BTC, LTC or GRC.
        size_bytes: the SERIALIZED size of the signed transaction, in bytes. The
            caller measures it; this function never guesses it. The upper bound
            it is sized from before a signature exists comes from
            modules/htlc_spend.estimated_script_sig_length() fed to
            ParsedTransaction.size_with_script_sig(), and
            modules/htlc_rpc.build_hashlock_spend() asserts the real size
            against that bound afterwards.

            THIS PARAGRAPH NAMED `modules/htlc_spend.estimated_signed_size()`
            until 2026-09-25 and there has never been a function of that name
            anywhere in the tree (verified by walking every def in the
            package). A reader who went looking found nothing and had to
            reconstruct the two-call answer above from the call site.

    Raises:
        ValueError: if the size is not positive, or the asset is unknown, or an
            environment override is malformed.
    """
    if size_bytes <= 0:
        raise ValueError(
            f"size_bytes must be positive, got {size_bytes!r}. A fee sized from a zero-byte transaction is a "
            "fee sized from nothing, which is how the flat fee got here in the first place."
        )
    rate = fee_rate_coin_per_kvb(asset)
    by_size = (rate * Decimal(size_bytes) / BYTES_PER_KVB).quantize(SATOSHI, rounding=ROUND_UP)
    return max(by_size, minimum_fee_coin(asset))


def effective_rate_coin_per_kvb(fee: Decimal, size_bytes: int) -> Decimal:
    """What a given fee over a given size actually works out to, in coin per kvB.

    This is the number the node compares against `maxfeerate`, so it is the
    number worth printing beside a fee -- rule 14: state what the number means,
    next to the number.
    """
    if size_bytes <= 0:
        raise ValueError(f"size_bytes must be positive, got {size_bytes!r}")
    return (fee * BYTES_PER_KVB / Decimal(size_bytes)).quantize(SATOSHI, rounding=ROUND_UP)


def assert_within_broadcast_ceiling(asset: str, fee: Decimal, size_bytes: int) -> Decimal:
    """Refuse, before broadcasting, a fee the node would refuse on arrival.

    Returns the effective rate so the caller can report it.

    This cannot fire with the table above -- 0.0003 coin/kvB is 333x under the
    ceiling, and the floor only exceeds it on a transaction under one byte.
    Neither could the flat fee it replaced, which is the point: this exists to
    catch an environment override somebody types in a hurry while a contract is
    open, which is the one path by which a fee rate here can become arbitrary.

    It is checked HERE, before the transaction is broadcast, rather than read
    off the node's rejection: a refusal that arrives from the daemon is a
    round trip and a log line an operator has to interpret, and on a chain
    whose sendrawtransaction takes no maxfeerate argument at all (Gridcoin's
    does not) no refusal arrives and the fee is simply paid.
    """
    rate = effective_rate_coin_per_kvb(fee, size_bytes)
    if rate > BROADCAST_CEILING_COIN_PER_KVB:
        raise ValueError(
            f"{asset}: a fee of {fee} over {size_bytes} bytes is {rate} coin/kvB, above sendrawtransaction's "
            f"default maxfeerate of {BROADCAST_CEILING_COIN_PER_KVB} coin/kvB. The node would refuse this as "
            "absurdly-high-fee. Nothing was broadcast."
        )
    return rate


def describe_fee(asset: str, fee: Decimal, size_bytes: int) -> str:
    """One line, self-describing a day later, for a log or a pasted diagnostic.

    Echoes the parameters that decided the answer -- the chain, the size, the
    rate and the floor -- because pasted output is usually read a day later
    (rule 14).
    """
    return (
        f"{asset} redeem miner fee={fee} over {size_bytes} bytes "
        f"= {effective_rate_coin_per_kvb(fee, size_bytes)} coin/kvB "
        f"(rule: max(rate {fee_rate_coin_per_kvb(asset)} coin/kvB x size, floor {minimum_fee_coin(asset)}); "
        f"node refuses above {BROADCAST_CEILING_COIN_PER_KVB} coin/kvB)"
    )
