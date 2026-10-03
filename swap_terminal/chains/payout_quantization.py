"""The amount a chain will ACTUALLY send, for one asset and one requested figure.

Role: function level (the bottom of CLAUDE.md rule 10's stack -- one dispatcher
      over five pure conversions; nothing opens a socket, reads a file or looks
      at the environment)
Reads: nothing. Arguments only.
Writes: nothing
Can move funds: no. It computes a number. But that number is what the payout
      path RECORDS, RESERVES against and then hands to the adapter, and the
      adapter re-derives the same number from it -- so it is on the money path
      by consequence rather than by capability. Nothing here signs or sends.
Mainnet-safe: yes.

=============================================================================
WHY THIS EXISTS, MEASURED 2026-10-03 ON A REAL COMPLETED SWAP
=============================================================================

The `payouts` row recorded the amount the QUOTE computed, not the amount the
CHAIN sent. Measured on swap s_539d922e9ef0a5d8 (GRC -> XRP), by reading the
XRP testnet ledger for the transaction the row names:

    payouts.amount    3.3155893288590605 XRP
    the ledger        Amount 3315589 drops = 3.315589 XRP, Fee 10,
                      tesSUCCESS, validated true

The record overstated the payment by 0.00000032885906 XRP. That is less than
one drop, so it is not a rounding preference -- the ledger CANNOT express the
number the database claims was paid.

HOW THE TWO NUMBERS CAME APART. services/payout_service.payout_amount() returns
the booked figure, _record_broadcast() writes THAT into `payouts`, and the
quantization happened later and deeper, inside the adapter:

    BTC / LTC / GRC   chains/coin_amounts.fit_to_chain_precision(), 8 decimals,
                      called inside chains/base.RPCAdapter.send_to_address()
    XRP               chains/xrp_units.to_drops(), 6 decimals, called inside
                      chains/xrp.XRPAdapter.preview_payout()
    SOL               chains/coin_amounts.amount_to_base_units() at
                      SOL_DECIMALS, called inside
                      chains/solana.SolanaAdapter.build_transfer_plan()

and every one of those adapters returns only a txid, so the service could not
learn what was actually sent even in principle.

IT WAS NEVER XRP-SPECIFIC. The same day, on the same host:

    LTC payout        booked 1.1996736819422498 LTC, broadcast 1.19967368 LTC
    BTC payout        broadcast 0.00401780 BTC
    GRC payout        booked 2701.3495803173805 GRC, broadcast 2701.34958031
                      (the daemon accepted the long figure and rounded on chain,
                      which is why five GRC swaps settled without anybody
                      finding this)

THE FIX IS TO QUANTIZE ONCE, BEFORE THE SEND, so the recorded number and the
broadcast number are the same number BY CONSTRUCTION rather than by
coincidence. That requires exactly one property, and it is measured rather than
assumed (rule 17): quantizing an already-quantized amount must return it
unchanged, because the adapter quantizes AGAIN after the service has. Measured
over 60,016 amounts spanning 1e-9 to 1e7 plus every figure named above,
tests/test_payout_quantization.py re-runs it:

    BTC / LTC / GRC   60,016 cases, 0 non-idempotent
    XRP               60,016 cases, 0 non-idempotent
    SOL               60,016 cases, 0 non-idempotent

So the figure this function returns is a FIXED POINT of the adapter's own
conversion, which is what makes the record match the chain without changing by
one base unit what any customer receives.

=============================================================================
WHY THIS IS NOT IN chains/coin_amounts.py, WHICH IS WHERE IT BELONGS
=============================================================================

coin_amounts.py is the natural home: it already owns CHAIN_DECIMALS,
fit_to_chain_precision() and amount_to_base_units(). It cannot hold this
dispatcher, and the reason is an import cycle that was MEASURED rather than
feared -- chains/solana_units.py line 153 does

    from .coin_amounts import amount_to_base_units as amount_to_base_units

so coin_amounts is BELOW solana_units in the import graph. A dispatcher in
coin_amounts needs SOL_DECIMALS and base_units_to_amount() from solana_units,
which closes the loop. Reproduced in both directions on 2026-10-03 with a
two-file package of the same shape:

    import coin_amounts first    ImportError: cannot import name
                                 'amount_to_base_units' from partially
                                 initialized module
    import solana_units first    ImportError: cannot import name
                                 'SOL_DECIMALS' from partially initialized
                                 module

The three ways to put it there anyway were each rejected, and which one was
rejected for which reason is the part worth keeping:

  a function-level import    works, and hides the dependency from every reader
                             and from the import graph a future reader greps.
  moving SOL_DECIMALS into   a Solana constant in a chain-agnostic module, and
  coin_amounts              coin_amounts' own docstring argues at length that
                             SOL is absent from CHAIN_DECIMALS on purpose.
  a second spelling of 6     rule 8's defect with a delay on it. XRP_DECIMALS
  and 9 in a new table       and SOL_DECIMALS already exist, in the modules
                             that own those chains.

So this module sits ONE LEVEL ABOVE the three conversions it dispatches to,
which is the only placement with a one-way import graph and is also what rule
10 asks for: the per-chain arithmetic is the function layer, and the thing that
picks between five of them is its caller.

    coin_amounts  <--  solana_units  <--  payout_quantization  -->  xrp_units
          ^                                      |
          +--------------------------------------+

=============================================================================
ONE CHAIN ROUNDS UP AND FOUR ROUND DOWN
=============================================================================

This is the one place that sees all five conversions at once, so it is where
the asymmetry between them is recorded. It is NOT introduced here and it is not
changed here:

    BTC / LTC / GRC   fit_to_chain_precision() TRUNCATES. The desk is never
    SOL               over; the customer is short by at most one base unit.
    XRP               to_drops() uses ROUND_HALF_UP, so a payout can be
                      rounded UP to the next whole drop.

Measured 2026-10-03 over 60,000 random amounts between 1e-9 and 1e7 XRP:
24,850 of them -- 41.4% -- quantize UPWARD, by at most half a drop
(0.0000005 XRP). Two of the figures named above are examples in each direction:

    0.0040178           -> 4018 drops = 0.004018     UP   by 0.2 drops
    1.1996736819422498  -> 1199674 drops = 1.199674  UP   by 0.26 drops
    3.3155893288590605  -> 3315589 drops = 3.315589  DOWN by 0.33 drops

THAT IS LEFT EXACTLY AS IT IS, and the reason is rule 16 rather than
indifference. to_drops() is what chains/xrp.py already calls, so ROUND_HALF_UP
is what the ledger has already been receiving; changing it to truncate would
change what gets sent, which is live posture and the operator's call. What this
module does is make the RECORD agree with it. The same difference is named at
chains/coin_amounts.fit_to_chain_precision() and at
chains/xrp_units.to_drops(), per rule 8: a reader who finds one must be told
the other exists.
"""

from __future__ import annotations

from decimal import Decimal

from .coin_amounts import CHAIN_DECIMALS, amount_to_base_units, fit_to_chain_precision
from .solana_units import SOL_DECIMALS, base_units_to_amount
from .xrp_units import DROPS_PER_XRP, from_drops, to_drops


def _quantize_bitcoin_family(amount: float, asset: str) -> tuple[float, str]:
    """BTC, LTC and GRC: whatever chains/base.RPCAdapter.send_to_address() will do.

    It CALLS fit_to_chain_precision() rather than repeating its arithmetic,
    which is the property this whole module depends on: the adapter calls the
    same function on the same input, so the second call cannot disagree with the
    first (rule 8 -- deriving one from the other beats maintaining both).
    """
    return fit_to_chain_precision(amount, asset)


def _quantize_xrp(amount: float, asset: str) -> tuple[float, str]:
    """XRP: whatever chains/xrp.XRPAdapter.preview_payout() will compute in drops.

    THE SAME to_drops() THE ADAPTER CALLS, including its ROUND_HALF_UP -- see
    this module's docstring for the measurement of how often that rounds a
    payout up, and why it is not changed here.

    The note names the DROP COUNT and not only the XRP figure, because drops are
    what the ledger's `Amount` field carries and what an operator comparing this
    row against a transaction will be reading (rule 14: state what the number
    means, next to the number).
    """
    drops = to_drops(amount)
    quantized = from_drops(drops)
    if quantized == amount:
        return amount, ""
    difference = Decimal(str(amount)) - Decimal(str(quantized))
    direction = "DOWN" if difference > 0 else "UP"
    return quantized, (
        f"{asset} sends integer drops, so {amount!r} is {drops} drops = {quantized!r} {asset} "
        f"-- rounded {direction} by {abs(difference)} {asset} ({abs(difference) * DROPS_PER_XRP} "
        f"drops). chains/xrp_units.to_drops() rounds HALF UP, which is the one conversion in this "
        f"terminal that can send more than was asked for; the other four truncate"
    )


def _quantize_solana(amount: float, asset: str) -> tuple[float, str]:
    """SOL: whatever chains/solana.SolanaAdapter.build_transfer_plan() will compute in lamports.

    NATIVE SOL'S NINE DECIMALS, AND THAT IS NOT A GUESS ABOUT SPL TOKENS. The
    payout path refuses an SPL mint outright -- SolanaAdapter.preview_payout()
    raises SolanaSplSendRefused before any network call -- so the only Solana
    amount this terminal can broadcast is native, at SOL_DECIMALS. A mint's own
    `decimals` is read from the chain by mint_decimals() and is not knowable
    from a pure function, which is exactly why this one does not pretend to
    cover that case.
    """
    base_units = amount_to_base_units(amount, SOL_DECIMALS)
    quantized = base_units_to_amount(base_units, SOL_DECIMALS)
    if quantized == amount:
        return amount, ""
    return quantized, (
        f"{asset} sends integer lamports, so {amount!r} is {base_units} lamports = {quantized!r} "
        f"{asset} -- truncated DOWN by {Decimal(str(amount)) - Decimal(str(quantized))} {asset}, "
        f"inheriting chains/coin_amounts.amount_to_base_units()'s direction so the desk never "
        f"sends more than it quoted"
    )


#: asset -> the function that answers "what will this chain actually send".
#:
#: THE BITCOIN FAMILY IS DERIVED FROM CHAIN_DECIMALS rather than listed, so a
#: fourth Bitcoin-style chain added to that table is covered here the same day
#: (rule 11's test: "did every consumer follow automatically? If a chain had to
#: be added to a second place by hand, that second place is the bug"). XRP and
#: SOL are named individually because their conversions are not that table's --
#: they are absent from it on purpose, which coin_amounts.py's docstring states.
#:
#: `dict.fromkeys` rather than a comprehension because ruff's C420 asks for it
#: and it is right: every Bitcoin-style chain maps to the SAME function, so the
#: comprehension's loop variable was never used and said otherwise.
QUANTIZERS = dict.fromkeys(CHAIN_DECIMALS, _quantize_bitcoin_family) | {
    "XRP": _quantize_xrp,
    "SOL": _quantize_solana,
}


def quantize_for_chain(amount: float, asset: str) -> tuple[float, str]:
    """`amount` as `asset`'s chain will really send it. (amount, "" or what changed).

    THE DECISION THIS MODULE EXISTS FOR, and it is the figure a payout records,
    reserves against and sends. One function, five chains, each dispatching to
    the conversion its own adapter already performs -- see this module's
    docstring for the measurement that started it and for why this file is not
    chains/coin_amounts.py.

    IDEMPOTENT ON EVERY CHAIN, MEASURED AND NOT ARGUED: quantizing an
    already-quantized amount returns it unchanged, over 60,016 amounts per chain
    in tests/test_payout_quantization.py. That is the property that makes this
    safe to put BEFORE the adapter, which will quantize again -- the adapter's
    second pass finds a fixed point and the broadcast amount is unchanged.

    A NON-POSITIVE AMOUNT IS RETURNED UNTOUCHED, WITH A SENTENCE, AND DOES NOT
    RAISE. The three conversions disagree about what to do with one:
    to_drops() raises XRPUnitError on a negative, fit_to_chain_precision()
    truncates it toward zero, and amount_to_base_units() does the same. None of
    those answers is useful, because there is no payment to describe -- and a
    raise here would be the expensive one. services/payout_service.
    amount_decided_and_logged() calls this OUTSIDE the per-swap try block, so an
    exception would abort process_pending_payouts() and leave every OTHER
    pending swap unpaid, where today a bad amount fails one swap and the loop
    continues. The adapters already refuse a non-positive send, each naming its
    own cause, and they stay the place that does.

    A POSITIVE AMOUNT THAT QUANTIZES TO NOTHING IS REPORTED AS 0.0, NOT
    REFUSED. 1e-09 BTC is a tenth of a satoshi and 1e-10 SOL is a tenth of a
    lamport; both are real inputs and neither is sendable. What to DO about it
    is the caller's, and payout_service deliberately does not substitute the
    zero -- it keeps the requested figure so that
    chains/base.RPCAdapter.send_to_address()'s existing refusal ("fits to 0.0
    ... which is nothing") fires with the cause in it. Deciding that here would
    put a payout policy inside a unit conversion.

    AN UNKNOWN ASSET IS RETURNED UNTOUCHED, WITH A SENTENCE, matching
    fit_to_chain_precision()'s handling of the same case and for the same
    reason: guessing a precision for a chain nobody listed is how a payout comes
    to be silently wrong. The hole is closed from the other end instead --
    tests/test_payout_quantization.py asserts that every destination asset in
    Config.ALLOWED_PAIRS has an entry in QUANTIZERS, so enabling a pair this
    module does not cover fails the suite rather than recording a figure the
    chain never sent.

    THE SECOND RETURN VALUE IS FOR THE LOG, not for a decision -- the same
    contract fit_to_chain_precision() already has, so a caller that already
    handles one handles this. A payout whose recorded figure differs from the
    quoted one has to be explainable from the row a year later.
    """
    requested = float(amount)
    if not requested > 0:
        return requested, (
            f"{requested!r} is not a positive amount, so there is nothing for {asset}'s unit "
            f"conversion to describe and it was NOT applied. The adapter refuses a non-positive "
            f"send and names its own cause"
        )
    quantizer = QUANTIZERS.get(asset)
    if quantizer is None:
        return requested, (
            f"{asset} has no entry in chains/payout_quantization.QUANTIZERS, so the amount was "
            f"returned UNCHANGED rather than quantized to a guessed precision. The recorded figure "
            f"may therefore differ from what the chain sends -- which is the defect this module "
            f"exists to remove, so an asset reaching here is a gap in QUANTIZERS and not a "
            f"property of the asset"
        )
    return quantizer(requested, asset)
