"""The amount this terminal will send a chain, for one asset and one requested figure.

IT SAID "the amount a chain will ACTUALLY send" UNTIL 2026-10-03 AND THAT WAS
FALSE FOR GRC. The figure returned is what the terminal HANDS the chain, which
is the same thing only because the send is quantized first. Handed an
over-precise amount directly, Gridcoin's daemon rounds half up and would send
one satoshi MORE than this function reports -- measured on 9 of 9 of the
operator's GRC payout rows, see WHAT EACH DAEMON DOES below. The old sentence is
kept here rather than deleted because the distinction it blurred is the subject
of half this header.

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
ALL FIVE TRUNCATE. ONE OF THEM DID NOT UNTIL 2026-10-03
=============================================================================

This is the one place that sees all five conversions at once, so it is where
the comparison between them is recorded.

    BTC / LTC / GRC   chains/coin_amounts.fit_to_chain_precision()  TRUNCATES
    SOL               chains/coin_amounts.amount_to_base_units()    TRUNCATES
    XRP               chains/xrp_units.to_drops()                   TRUNCATES
                      (ROUND_HALF_UP until 2026-10-03)

OPERATOR DECISION 2026-10-03, in their words: "make it all match". XRP's
to_drops() was the one conversion that did not, and it became ROUND_DOWN. That
changes what the XRP Ledger receives -- by at most one drop, downward -- and it
is the operator's call, which they made; the argument for the direction is in
to_drops()' own docstring and is that chains/coin_amounts.py already DOCUMENTS
the invariant "the desk never sends more than it quoted", which truncation makes
true on all five chains instead of carrying a caveat for one.

MEASURED after the change, over the seeded 60,000-draw sample in
tests/test_payout_quantization._sample_amounts() -- 60,015 positive amounts once
its 15 named positive figures are counted in:

    chain       amounts quantizing UPWARD   non-idempotent
    BTC                 0 of 60,015              0 of 60,015
    LTC                 0 of 60,015              0 of 60,015
    GRC                 0 of 60,015              0 of 60,015
    SOL                 0 of 60,015              0 of 60,015
    XRP                 0 of 60,015              0 of 60,015

and before it, on the identical sample, XRP read 24,854 of 60,015 upward
(24,850 of the 60,000 random draws plus 4 of the 15 named positives -- the two
halves reconcile exactly, which is why the old 24,850-of-60,000 figure in the
superseded text below is the same measurement and not a different one).

THE SUPERSEDED SECTION IS KEPT RATHER THAN OVERWRITTEN (rule 1). It was true
when it was written, five hours before the operator decided otherwise, and a
reader who finds the new text should be able to see what it replaced:

> ONE CHAIN ROUNDS UP AND FOUR ROUND DOWN
>
> This is the one place that sees all five conversions at once, so it is where
> the asymmetry between them is recorded. It is NOT introduced here and it is
> not changed here:
>
>     BTC / LTC / GRC   fit_to_chain_precision() TRUNCATES. The desk is never
>     SOL               over; the customer is short by at most one base unit.
>     XRP               to_drops() uses ROUND_HALF_UP, so a payout can be
>                       rounded UP to the next whole drop.
>
> Measured 2026-10-03 over 60,000 random amounts between 1e-9 and 1e7 XRP:
> 24,850 of them -- 41.4% -- quantize UPWARD, by at most half a drop
> (0.0000005 XRP). [...]
>
> THAT IS LEFT EXACTLY AS IT IS, and the reason is rule 16 rather than
> indifference. to_drops() is what chains/xrp.py already calls, so
> ROUND_HALF_UP is what the ledger has already been receiving; changing it to
> truncate would change what gets sent, which is live posture and the
> operator's call.

The invariant now holds on the way out of this function on every chain: the
figure returned is never greater than the figure passed in.

=============================================================================
WHAT EACH DAEMON DOES WITH AN OVER-PRECISE AMOUNT -- FOUR BEHAVIORS, AND THE
TREE MODELED ONE OF THEM
=============================================================================

THIS IS A DIFFERENT QUESTION FROM THE SECTION ABOVE, and conflating the two is
what produced a false claim that stood in this file for a day. Above is what
THIS TERMINAL computes. Below is what the far end does if it is ever handed a
figure with more decimals than it can express -- which is a property of someone
else's parser, not of our arithmetic, and the two do not agree.

    BTC, LTC          REJECT IT. Modern Bitcoin Core parses an amount with
    (modern Core)     ParseFixedPoint(value, 8), which refuses more than eight
                      decimals rather than rounding: rpc error code -3,
                      "Invalid amount". Measured on the operator's host
                      2026-10-03 with `bitcoin-cli createrawtransaction`, which
                      runs the same parser and broadcasts nothing --
                      0.00041198765432109 was refused. The same daemon ACCEPTED
                      exponent notation (4.1e-07, an output of 41 satoshis), so
                      the defect is the decimal COUNT alone. Those two readings
                      are the operator's and are recorded at length at
                      chains/coin_amounts.py's header and at
                      chains/base.RPCAdapter.send_to_address(); they are not
                      repeated here.
    GRC (pre-0.17)    ROUNDS IT, HALF UP. Gridcoin is Bitcoin-derived from
                      before the ParseFixedPoint change, so its RPC takes the
                      amount through a C++ double and rounds. Measured on the
                      operator's host 2026-10-03 by reading each payout
                      transaction off their own daemon: of their 9 GRC payout
                      rows, 9 OF 9 match ROUND_HALF_UP at eight decimals and
                      only 6 of 9 match truncation -- and the 6 are exactly the
                      rows whose remainder beyond the eighth decimal is below
                      0.5, where the two roundings cannot differ. The three that
                      discriminate, as their chain reports them:

                          payouts.id=6   recorded 82.65089987734812
                                         chain    82.65089988
                                         truncated 82.65089987
                          payouts.id=7   recorded 87.97839509528555
                                         chain    87.9783951
                                         truncated 87.97839509
                          payouts.id=17  recorded 2701.3495803173805
                                         chain    2701.34958032
                                         truncated 2701.34958031

                      One satoshi of GRC each, three in total: negligible as
                      money and wrong as a model, which is the only reason it is
                      written down.
    XRP (xrpl-py)     ROUNDS IT. to_drops() reaches the ledger as an integer
                      drop count, so no over-precise decimal is ever parsed by
                      rippled -- the rounding happens on this side. Until
                      2026-10-03 it rounded HALF UP, which is the measurement in
                      the superseded block above.
    SOL               NEVER PARSES ONE. The lamport count is computed by
                      chains/coin_amounts.amount_to_base_units() in this tree's
                      own code and reaches the cluster as an integer, so the
                      cluster is never given a decimal amount to interpret.

THE CAUSE OF THE GRC BEHAVIOR WAS VERIFIED IN GRIDCOIN'S OWN SOURCE rather
than inferred from "it is old Bitcoin code" (rule 17). At tag 5.5.1.0, which is
the version the operator runs:

    src/wallet/rpcwallet.cpp:396   sendtoaddress -> AmountFromValue(params[1])
    src/rpc/server.cpp:105-114     AmountFromValue:
                                     double dAmount = value.get_real();
                                     int64_t nAmount = roundint64(dAmount * COIN);
    src/util.h:155-158             roundint64(d) -> (int64_t)(d > 0 ? d + 0.5
                                                                   : d - 0.5)

`(int64_t)(d + 0.5)` for a positive d is truncation of d plus a half, which is
round-half-up. For comparison, Bitcoin Core v0.17.0's src/rpc/server.cpp:106-116
is the other branch of the same function's history and calls
ParseFixedPoint(value, 8), returning "Invalid amount" instead.

WHAT THE 9 ROWS DO *NOT* ESTABLISH, said plainly because the two models are
easy to conflate. Decimal's ROUND_HALF_UP and Gridcoin's
roundint64(double * COIN) are not the same function: they differ wherever the
double `d + 0.5` lands on the far side of the integer, which is to say on
amounts that are exact decimal ties at the ninth decimal. Measured here
2026-10-03: 4 of 200,000 random amounts in [0, 4000) distinguish them (for
instance 3180.276700225, where Decimal gives ...23 and the double gives ...22),
and 176,171 of 200,000 amounts of the form k.000000005 do. NONE of the
operator's 9 rows is such a tie, so those rows prove the daemon ROUNDS TO
NEAREST and do not prove which of the two implementations does it. The source
reading above is what establishes the implementation; the rows establish the
behavior.

THIS TABLE IS NOT DEAD CODE AND MUST NOT BE DELETED AS SUCH. Since 54892d5 the
payout service quantizes BEFORE the send, so the adapter -- and therefore every
daemon above -- receives a figure already at its chain's precision, and none of
the four behaviors is reachable from the payout path any more. That is exactly
why the table has to stay: it is the record of what would happen if that
ordering were ever undone, and of what DID happen to the three GRC rows that
were sent before it existed. Those three are still refused by
correct_payout_amounts.py, because the quantizer still truncates and the chain
still rounded; the flag
correct_payout_amounts.py --trust-the-chain-over-the-quantizer is what resolves
them, and "we made it all match" did not.

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

    THE SAME to_drops() THE ADAPTER CALLS, which TRUNCATES since 2026-10-03 --
    see this module's docstring for the operator decision that changed it and
    for the before/after measurement.

    THE "UP" BRANCH WAS DELETED WITH THAT CHANGE, and it is named here so the
    deletion is not read as an oversight (rule 9). This function used to compute
    a direction, because to_drops() rounded half up and the difference could fall
    either way. Under truncation it cannot: to_drops() returns floor(amount *
    1_000_000), so the exact quotient is never above `amount`, and from_drops()
    cannot push it above either -- it divides by 1e6 to the NEAREST double, and
    no double nearer to a value <= `amount` lies above `amount`, since `amount`
    is itself a double at that distance or closer. Measured as well as argued:
    0 of 60,015 positive sample amounts come back larger
    (tests/test_payout_quantization.py). So "DOWN" is the only reachable word and
    a computed `direction` would be a branch that can only print one value.

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
    return quantized, (
        f"{asset} sends integer drops, so {amount!r} is {drops} drops = {quantized!r} {asset} "
        f"-- truncated DOWN by {difference} {asset} ({difference * DROPS_PER_XRP} drops). "
        f"chains/xrp_units.to_drops() truncates, as all five conversions in this terminal do since "
        f"2026-10-03, so no payout exceeds its quote"
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
        f"sends more than it quoted -- the direction all five conversions share since 2026-10-03"
    )


#: asset -> the function that answers "what will this terminal hand this chain".
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
    """`amount` as `asset`'s adapter will send it. (amount, "" or what changed).

    THE DECISION THIS MODULE EXISTS FOR, and it is the figure a payout records,
    reserves against and sends. One function, five chains, each dispatching to
    the conversion its own adapter already performs -- see this module's
    docstring for the measurement that started it and for why this file is not
    chains/coin_amounts.py.

    "AS THE ADAPTER WILL SEND IT", NOT "AS THE CHAIN WILL SEND IT", and the
    correction is 2026-10-03's. The two coincide only because the send is
    quantized before it is made; a daemon handed an over-precise figure directly
    may answer differently, and Gridcoin's measurably does. The four-behavior
    table in this module's header is that distinction written out.

    NEVER GREATER THAN THE AMOUNT PASSED IN, on any of the five chains, since
    XRP's to_drops() became truncating on 2026-10-03. Measured over 60,015
    positive amounts per chain in tests/test_payout_quantization.py: 0 quantize
    upward. Before that change XRP returned a larger figure for 24,854 of the
    same 60,015.

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
