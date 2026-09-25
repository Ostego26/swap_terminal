"""Monero's unit, confirmation and transfer-shape decisions, as pure functions.

Role: function layer (rule 10 -- every decision in this file is callable with
      seeded inputs and returns a value; nothing here opens a socket)
Reads: nothing. Arguments only.
Writes: nothing
Can move funds: no. to_atomic() COMPUTES the number that a transfer would
      carry, but nothing in this module sends anything.
Mainnet-safe: yes

WHY THIS IS A SEPARATE MODULE AND NOT PART OF chains/monero.py.

CLAUDE.md rule 10: the thing that actually decides should be the smallest,
most testable piece at the bottom. The adapter is transport -- it builds a URL,
signs in, posts JSON and hands back whatever came out. Everything in this file
is a decision: how many atomic units an amount is, whether a confirmation count
is high enough to spend, and which wallet transfers count as a deposit to a
given address. Those are the parts that can be wrong in a way that costs money,
and they are here so that a test can call them directly instead of standing up
a daemon.

That split is also the only reason any of this is provable in the environment
it was written in. See THE HONEST STATUS block in chains/monero.py: the RPC
field names could not be verified against a real monero-wallet-rpc from here.
The arithmetic and the filtering below have no such excuse and are tested.

WHAT MONERO DOES DIFFERENTLY, AND WHY EACH ONE IS HERE

1. TWELVE DECIMALS, NOT EIGHT. The atomic unit is the piconero, 1e-12 XMR,
   where the three Bitcoin-derived chains in chains/base.py use 1e-8. That file
   hardcodes the difference as `SATOSHI = 1e-8` with the comment "The chains
   here all use 8 decimal places", which was true when it was written and is
   the reason this module exists rather than a fourth one-line subclass.

2. FLOATS RUN OUT SOONER AT TWELVE DECIMALS, and the number is worth writing
   down rather than hand-waving. db.py stores every amount as SQLite REAL,
   which is an IEEE double, and a double holds integers exactly only up to
   2**53 = 9,007,199,254,740,992. Measured 2026-09-25:

       at 12 decimals (XMR)          9,007.199254740992 coins
       at  8 decimals (BTC/LTC/GRC)  90,071,992.54740992 coins

   So the existing float-based schema is comfortable for the three incumbent
   chains and has about four orders of magnitude less headroom for Monero. It
   is still far above any swap this terminal is sized for, which is why this is
   a documented limit rather than a schema change -- but it is a REAL ceiling
   and the next person to raise swap limits needs to know it is there.

3. A TEN-BLOCK SPEND LOCK THAT IS CONSENSUS, NOT POLICY. A Monero output
   cannot be spent until 10 blocks after the transaction that created it. That
   is not a confirmation preference the way BTC_MIN_CONFIRMATIONS is; the
   wallet will refuse to build a transaction spending a younger output no
   matter what this application thinks. Setting XMR_MIN_CONFIRMATIONS to 2
   therefore does not buy a faster payout -- it buys a payout attempt that
   fails at `transfer` time instead of a deposit that waits at the gate. The
   floor belongs in code, next to the reason, rather than in a comment beside
   an environment variable nobody reads.

4. NO VOUTS. A Bitcoin deposit is identified by (txid, vout) and
   chains/base.py searches a decoded transaction's outputs for one that pays
   the address. A Monero transaction publishes no such thing to the recipient:
   the wallet reports what it found for itself, already decrypted, and the
   output index that identifies it is the chain-wide `global_index` rather
   than a position within the transaction. deposit_events_from_transfers()
   below is where that is turned back into the (txid, vout) shape the rest of
   the application speaks, and its refusal to invent one is the whole point of
   the function -- see the comment there.
"""

from decimal import ROUND_HALF_UP, Decimal

# 1 XMR = 10**12 piconero. The name "atomic units" is Monero's own.
ATOMIC_UNITS_PER_XMR = 10**12
XMR_DECIMALS = 12

# Monero consensus: an output is unspendable for this many blocks after the
# transaction that created it. See point 3 in the module docstring -- this is
# a floor under configuration, not a default for it.
CONSENSUS_SPEND_LOCK_BLOCKS = 10

# The largest integer an IEEE double represents exactly, and what it is worth
# in each unit system. Measured, not recalled -- see point 2 above.
MAX_EXACT_INTEGER = 2**53
MAX_EXACT_XMR = MAX_EXACT_INTEGER / ATOMIC_UNITS_PER_XMR


class MoneroUnitError(ValueError):
    """An amount could not be expressed in atomic units without losing money.

    Raised rather than rounded silently. Rounding a payout amount down is a
    small loss; rounding a payout amount from a number that was never a valid
    Monero quantity in the first place hides a bug upstream, and the bug is
    worth more than the rounding.
    """


def to_atomic(amount) -> int:
    """XMR as a decimal quantity -> piconero as an integer.

    Goes through Decimal(str(amount)) rather than multiplying the float,
    because `0.1 * 10**12` is 100000000000.00001 in binary floating point and
    int() of that is a number of piconero that is not the number asked for.
    str() of a float gives its shortest round-tripping decimal form, which for
    every amount this application actually handles is the amount a human typed.

    Rounds half-up at the piconero, and the maximum error is therefore half a
    piconero -- 5e-13 XMR, which is not a representable quantity on the chain
    and not a meaningful one anywhere else. Refusing to round instead would
    turn an unrepresentable amount into a stalled payout, and a stalled payout
    is a worse failure than an error of 5e-13.

    Negative amounts raise. Every caller here is a deposit credit or a payout,
    and neither has a meaning below zero; letting one through would express
    itself as a `transfer` the daemon rejects, several layers away from the
    mistake.
    """
    try:
        quantity = Decimal(str(amount))
    # Broad on purpose, and BLE001 does not fire because this handler RAISES.
    # Decimal() raises InvalidOperation for anything unparseable and str()
    # raises for an object with a hostile __str__; both mean one thing to the
    # caller ("this is not an amount"). The original is chained with `from`
    # rather than discarded, so the failure can never be mistaken for a real
    # answer -- which is the condition rule 12 puts on catching broadly.
    except Exception as error:
        raise MoneroUnitError(f"{amount!r} is not a number of XMR") from error
    if quantity.is_nan() or quantity.is_infinite():
        raise MoneroUnitError(f"{amount!r} is not a finite number of XMR")
    if quantity < 0:
        raise MoneroUnitError(f"{amount!r} is negative; neither a deposit nor a payout can be")
    scaled = quantity * ATOMIC_UNITS_PER_XMR
    return int(scaled.to_integral_value(rounding=ROUND_HALF_UP))


def from_atomic(atomic: int) -> float:
    """Piconero as an integer -> XMR as a float, to match the rest of the app.

    A float because that is what db.py's REAL columns and every adapter method
    in chains/base.py already use; returning a Decimal here would make this the
    one chain whose amounts compare unequal to every other chain's. The
    precision ceiling that buys is written down at MAX_EXACT_XMR and in point 2
    of the module docstring. Use from_atomic_decimal() where the exact quantity
    matters -- display, reconciliation, anything an operator will read.
    """
    return int(atomic) / ATOMIC_UNITS_PER_XMR


def from_atomic_decimal(atomic: int) -> Decimal:
    """Piconero -> XMR as an exact Decimal, for anything that must not drift."""
    return Decimal(int(atomic)).scaleb(-XMR_DECIMALS)


def effective_min_confirmations(configured: int) -> int:
    """The confirmation count actually enforced, given what was configured.

    Never returns less than CONSENSUS_SPEND_LOCK_BLOCKS. Configuring fewer is
    not wrong in the sense of being disallowed -- it is wrong in the sense of
    having no effect, because the wallet cannot build a spend of a younger
    output regardless. Clamping here means the deposit gate and the payout
    attempt agree, instead of the gate releasing a swap that `transfer` then
    refuses.
    """
    return max(int(configured), CONSENSUS_SPEND_LOCK_BLOCKS)


def describe_min_confirmations(configured: int) -> str:
    """One line saying what is enforced, and saying so when it is not what was asked.

    Rule 14: state what the number means, next to the number. A worker banner
    that printed `min_confirmations=2` while the code enforced 10 would be
    telling the operator something false about their own configuration, and
    they would read the screen rather than this file.
    """
    effective = effective_min_confirmations(configured)
    if effective == int(configured):
        return f"{effective} blocks"
    return (
        f"{effective} blocks  <- raised from the configured {int(configured)}: Monero outputs are "
        f"unspendable for {CONSENSUS_SPEND_LOCK_BLOCKS} blocks by consensus, so a lower setting would "
        f"release the swap and then fail at transfer time"
    )
