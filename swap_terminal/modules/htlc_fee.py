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
from decimal import ROUND_UP, Decimal, InvalidOperation

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
            caller measures it; this function never guesses it. See
            modules/htlc_spend.estimated_signed_size(), which computes an exact
            upper bound before the signature exists, and asserts against the
            real size afterwards.

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
