"""XRP's unit, reserve and finality decisions, as pure functions.

Role: function layer (rule 10 -- seeded inputs, returned values, no I/O)
Reads: nothing. Arguments only.
Writes: nothing
Can move funds: no. to_drops() computes the number a payment would carry.
Mainnet-safe: yes

WHAT XRP DOES DIFFERENTLY FROM EVERY CHAIN ALREADY HERE

1. SIX DECIMALS, FEWER THAN BITCOIN'S EIGHT. The base unit is the drop, 1e-6
   XRP. That makes XRP the roomiest chain in this tree for the REAL columns
   db.py stores amounts in: measured 2026-09-25, a double holds integers
   exactly to 2**53, which is 9,007,199,254 XRP against 90,071,992 for the
   8-decimal chains and 9,007 for Monero's twelve. XRP's total supply is under
   100 billion, so the ceiling is reachable in principle and unreachable by any
   broker; it is written down rather than relied on.

2. FINALITY IS BINARY, NOT A DEPTH. A validated ledger is final -- the XRP
   Ledger has no reorganizations, so there is no such thing as "six
   confirmations". A payment is either in a validated ledger or it is not.
   This is the same category mismatch chains/solana_units.py faced with
   commitment levels, and it is handled the same way: an explicit small ladder,
   named for what it is, rather than a fabricated depth count that would read
   like Bitcoin's and mean something else.

3. THE RESERVE IS A NETWORK PARAMETER AND MUST NOT BE HARDCODED. Every funded
   account must retain a base reserve, and it has CHANGED -- it was 20 XRP,
   then 10, then 1. A constant compiled in here would be wrong after the next
   amendment and would silently make sweeps fail or, worse, make the terminal
   believe it has spendable balance it does not. The adapter asks server_info.
   The figures below are labeled reference values for a banner, never a gate.
"""

from decimal import ROUND_HALF_UP, Decimal

DROPS_PER_XRP = 1_000_000
XRP_DECIMALS = 6

MAX_EXACT_INTEGER = 2**53
MAX_EXACT_XRP = MAX_EXACT_INTEGER / DROPS_PER_XRP

# THE FINALITY LADDER. Two rungs, because the ledger has two states and
# inventing a third would be dressing a boolean as a count.
LEDGER_UNVALIDATED = 0
LEDGER_VALIDATED = 1
MAX_LEDGER_RANK = LEDGER_VALIDATED

# REFERENCE VALUES ONLY, for a banner line. The live figures come from
# server_info.validated_ledger.reserve_base_xrp / reserve_inc_xrp. See point 3.
REFERENCE_BASE_RESERVE_XRP = Decimal(1)
REFERENCE_OWNER_RESERVE_XRP = Decimal("0.2")


class XRPUnitError(ValueError):
    """An amount could not be expressed in drops without losing money."""


class XRPThresholdError(ValueError):
    """A confirmation threshold was configured that no XRP payment can satisfy.

    Raised at construction rather than at poll time, and that timing is the
    point. A threshold of 6 asks for a rung that does not exist on this ledger,
    so every XRP deposit would sit below it forever -- a swap stuck permanently,
    with nothing in any log saying why, because nothing failed.
    """


def to_drops(amount) -> int:
    """XRP as a decimal quantity -> drops as an integer.

    Via Decimal(str(amount)) rather than multiplying the float, for the reason
    chains/monero_units.py documents at length: `0.1 * 10**6` is not reliably
    100000 in binary floating point, and the error falls whichever way the
    representation happens to land.
    """
    try:
        quantity = Decimal(str(amount))
    # Broad on purpose; BLE001 does not fire because this handler RAISES.
    # Decimal() raises InvalidOperation for anything unparseable and str()
    # raises for a hostile __str__; both mean "this is not an amount" to the
    # caller, and the original is chained rather than discarded.
    except Exception as error:
        raise XRPUnitError(f"{amount!r} is not a number of XRP") from error
    if quantity.is_nan() or quantity.is_infinite():
        raise XRPUnitError(f"{amount!r} is not a finite number of XRP")
    if quantity < 0:
        raise XRPUnitError(f"{amount!r} is negative; neither a deposit nor a payout can be")
    return int((quantity * DROPS_PER_XRP).to_integral_value(rounding=ROUND_HALF_UP))


def from_drops(drops) -> float:
    """Drops -> XRP as a float, matching what db.py's REAL columns hold.

    Accepts a string because that is how the XRP Ledger sends amounts: drop
    counts travel as JSON STRINGS, not numbers, precisely so a JavaScript
    client cannot round them through a double on the way in. Accepting the
    string here and converting once is what keeps that protection intact.
    """
    return int(drops) / DROPS_PER_XRP


def from_drops_decimal(drops) -> Decimal:
    """Drops -> XRP as an exact Decimal, for anything an operator reads."""
    return Decimal(int(drops)).scaleb(-XRP_DECIMALS)


def ledger_rank(validated) -> int:
    """A ledger's validation state as the integer the deposit gate compares.

    services/deposit_service.py releases a swap when the event's
    `confirmations` reaches `min_confirmations`, and that comparison is shared
    by every chain. XRP has no depth to report, so what it reports is this
    rank -- 1 when the transaction is in a validated ledger, 0 otherwise.

    Deliberately NOT a slot or ledger-index difference. A depth number would
    satisfy the same comparison while meaning something it does not mean here:
    on a ledger with no reorganizations, being ten ledgers deep is not safer
    than being validated, it is just later.
    """
    return LEDGER_VALIDATED if validated is True else LEDGER_UNVALIDATED


def validate_min_confirmations(configured: int) -> int:
    """Refuse a threshold no XRP payment can ever reach.

    chains/solana.py refuses SOL_MIN_CONFIRMATIONS=6 for exactly this reason
    and it is the same defect here: a number copied from a Bitcoin-shaped
    config silently makes every deposit un-creditable, forever, quietly.
    """
    value = int(configured)
    if value < LEDGER_VALIDATED or value > MAX_LEDGER_RANK:
        raise XRPThresholdError(
            f"XRP_MIN_CONFIRMATIONS={value} cannot be satisfied. The XRP Ledger does not reorganize, "
            f"so a payment is either in a validated ledger ({LEDGER_VALIDATED}) or it is not "
            f"({LEDGER_UNVALIDATED}) -- there is no depth to accumulate. A value copied from a "
            f"Bitcoin-shaped config would leave every XRP deposit below the threshold forever, with "
            f"nothing in any log saying why. Set it to {LEDGER_VALIDATED}."
        )
    return value


def describe_min_confirmations(configured: int) -> str:
    """The banner line, naming the unit so it cannot be read as blocks (rule 14)."""
    value = validate_min_confirmations(configured)
    return f"{value} validated ledger  <- NOT a block depth; the XRP Ledger does not reorganize"
