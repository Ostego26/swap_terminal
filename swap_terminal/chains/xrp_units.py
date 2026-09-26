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

4. THE DEPOSIT IDENTIFIER IS AN INTEGER, NOT AN ADDRESS. Every other chain in
   this tree hands out a fresh deposit address per swap. XRP does not need to:
   a `DestinationTag` on one shared account is the per-swap identifier, which
   is why chains/xrp.py::get_new_address() REFUSES and names a tag allocator in
   services/ as what is wanted instead. The tag's RANGE lives here, with the
   rest of the protocol's arithmetic, and services/xrp_tag_service.py is the
   only thing that allocates one -- one vocabulary, one place (rule 11).

   MEASURED 2026-09-26, in this container, against the installed xrpl-py
   (the reference implementation's own Python binding), because xrpl.org was
   not reachable from here and prior knowledge is not a measurement (rule 17):

       xrpl.core.binarycodec.types.uint32._WIDTH          4 bytes
       UInt32.from_value(0)                               ACCEPTED
       UInt32.from_value(4294967295)                      ACCEPTED
       UInt32.from_value(4294967296)                      OverflowError
       UInt32.from_value(-1)                              OverflowError

   and end to end through a whole transaction, which is the stronger form --
   a real Payment built between ACCOUNT_ZERO and ACCOUNT_ONE, serialized with
   `encode()` and decoded back:

       destination_tag=0            serialized, decoded back as DestinationTag=0
       destination_tag=1            serialized, decoded back as 1
       destination_tag=4294967295   serialized, decoded back as 4294967295
       destination_tag=4294967296   OverflowError: int too big to convert

   So MAX_DESTINATION_TAG below is 2**32 - 1 because that is what the
   serializer accepts and not because the protocol is remembered as uint32.
   tests/test_xrp_destination_tags.py re-runs that measurement against the
   installed codec and skips if xrpl-py is absent, so the constant cannot
   drift away from the thing it was measured from. xrpl-py stays OPTIONAL
   here: nothing in this module imports it, and the constant is a plain
   integer precisely so a signing library is not needed to allocate a tag.

   The third line of that measurement is also the reason tag 0 is reserved
   rather than used -- see RESERVED_DESTINATION_TAG below. 0 is a legal tag
   that travels on the wire and decodes back as present, which is exactly what
   makes it dangerous.
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

# THE DESTINATION TAG RANGE. Point 4 of this module's docstring has the
# measurement these two numbers came from; they are not recalled, they were run.
#
# MAX_DESTINATION_TAG is a hard protocol bound: a tag above it cannot be put on
# the wire at all, so a value above it in this application is a bug in this
# application and never something a ledger sent us.
MAX_DESTINATION_TAG = 2**32 - 1

# TAG 0 IS LEGAL AND IS DELIBERATELY NEVER ALLOCATED.
#
# The measurement above is explicit that 0 round-trips as `DestinationTag=0`,
# which is a DIFFERENT wire state from the field being absent -- and
# chains/xrp_payments.py::_classify() is correspondingly careful to test
# `tag is None` rather than truthiness, with a test pinning it. So nothing here
# may treat 0 as "no tag": a payment carrying 0 is a payment carrying a tag.
#
# What makes it unsafe to HAND OUT is the other side of that same fact. An
# integration that has no tag to send, but whose field is a non-optional
# integer, sends 0 -- that is the value an uninitialized int, an empty form
# field and a "0 means none" convention all produce. Every one of those
# payments is indistinguishable on the wire from a customer paying whichever
# swap owns tag 0.
#
# Reserving it costs one integer out of 4,294,967,295 and converts that entire
# class of mistaken payment from "credited to an unrelated swap" into "arrived
# with a tag nothing owns", which is the case
# chains/xrp_payments.py::deposit_events_from_transactions() already reports as
# deferred for an operator to match by hand. A misattributed deposit pays the
# wrong person and cannot be undone (see CLAUDE.md's opening section); an
# unattributed one is a support ticket.
#
# NOT measured, and stated as the hypothesis it is (rule 17): that real senders
# in the wild emit 0 as a placeholder. No traffic was surveyed for it here.
# What is measured is only that 0 is a valid, allocatable, wire-visible tag --
# which is enough, because the reservation costs nothing and the failure it
# avoids is irreversible.
RESERVED_DESTINATION_TAG = 0
FIRST_ALLOCATABLE_TAG = RESERVED_DESTINATION_TAG + 1

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


class XRPTagError(ValueError):
    """A destination tag was not a value this ledger can carry, or must not be used.

    Separate from XRPUnitError because the two are different failures with
    different consequences. A bad amount is caught before anything leaves; a
    bad TAG is an attribution failure, and attribution failures pay the wrong
    person. Naming them apart is what lets a caller report which happened
    instead of "something about this payment was wrong".
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


def validate_destination_tag(tag, *, allocatable: bool = True) -> int:
    """One destination tag, checked against the ledger and against our reservation.

    TWO DIFFERENT QUESTIONS, and the flag is which one is being asked, because
    conflating them would make one of the two answers wrong:

      allocatable=True (the default)   may THIS APPLICATION hand this tag out?
                                      Tag 0 is refused -- see
                                      RESERVED_DESTINATION_TAG above.
      allocatable=False               could a ledger have DELIVERED this tag?
                                      Tag 0 is accepted, because it is legal
                                      and real senders can set it.

    Getting that backwards in the lenient direction would let the allocator
    hand out 0. Getting it backwards in the strict direction would make the
    application refuse to look up a payment the ledger genuinely delivered,
    which loses a deposit rather than misattributing one.

    `bool` is rejected explicitly, matching chains/xrp_payments.py::_classify()
    and for the identical reason: `True == 1` in Python, so `isinstance(True,
    int)` is True and a stray boolean would silently become tag 1 -- a tag some
    real swap owns. There is a test pinning that in xrp_payments and there is
    one here.
    """
    if isinstance(tag, bool) or not isinstance(tag, int):
        raise XRPTagError(
            f"destination tag {tag!r} is {type(tag).__name__}, not an int. NOT coerced: a `bool` is an "
            f"int in Python (True == 1), so coercing would turn True into tag 1, which is a tag a real "
            f"swap owns. chains/xrp_payments.py::_classify() rejects bool at the reading end for the "
            f"same reason."
        )
    if tag < RESERVED_DESTINATION_TAG or tag > MAX_DESTINATION_TAG:
        raise XRPTagError(
            f"destination tag {tag} is outside the ledger's range "
            f"{RESERVED_DESTINATION_TAG}..{MAX_DESTINATION_TAG}. That bound is MEASURED, not recalled -- "
            f"see point 4 of this module's docstring: xrpl-py's own serializer raises OverflowError at "
            f"{MAX_DESTINATION_TAG + 1}, so a tag above it cannot be put on the wire and cannot have "
            f"come off it either."
        )
    if allocatable and tag == RESERVED_DESTINATION_TAG:
        raise XRPTagError(
            f"destination tag {RESERVED_DESTINATION_TAG} is LEGAL on the ledger and is deliberately "
            f"never allocated by this terminal. It is the value every 'no tag to send' integration emits "
            f"-- an uninitialized int, an empty form field, a 0-means-none convention -- and a payment "
            f"carrying it would otherwise be credited to whichever swap owns it. Reserved so that such a "
            f"payment arrives UNATTRIBUTED and reaches an operator instead. Pass allocatable=False to "
            f"validate a tag that was RECEIVED rather than one being handed out."
        )
    return tag


def describe_min_confirmations(configured: int) -> str:
    """The banner line, naming the unit so it cannot be read as blocks (rule 14)."""
    value = validate_min_confirmations(configured)
    return f"{value} validated ledger  <- NOT a block depth; the XRP Ledger does not reorganize"
