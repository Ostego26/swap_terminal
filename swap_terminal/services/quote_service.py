"""Turn a price and an amount into a recorded quote.

Role: submodule -> function (create_quote is the decision)
Reads: config (fee bps, TTL, allowed pairs, per-asset fee reserve), CoinGecko
       through services/pricing.py
Writes: swap_terminal.db (quotes)
Can move funds: no, and yes one step removed. output_amount_estimate computed
       here is the exact figure services/payout_service.py later broadcasts.
       The fee arithmetic in create_quote() is therefore a fund-moving
       decision and changing it is the operator's (rule 16).
Mainnet-safe: yes

validate_pair() is the gate that stops an unsupported direction from ever
reaching a swap. It reads ALLOWED_PAIRS from config rather than carrying its
own list, so there is one vocabulary (rule 11).
"""

import logging
import time
from datetime import timedelta

from chains.solana_signing import below_new_account_floor
from chains.solana_units import SOL_DECIMALS, SYSTEM_ACCOUNT_SPACE, amount_to_base_units, decimal_amount

from .helpers import new_id, utc_now
from .pricing import derive_pair_rate, fetch_usd_prices, last_price_source

logger = logging.getLogger(__name__)

#: SECONDS a cluster's rent-exempt minimum is reused for. Seconds because it is
#: a `time.monotonic()` difference (rule 6: seconds stay where the interface
#: demands them, and the conversion happens at the print -- nothing prints this
#: one, it only compares).
#:
#: WHY A CACHE AT ALL, and why this long. The figure is a CLUSTER parameter: it
#: changes only when a feature gate activates, and chains/solana_units.py
#: records the last such change (SIMD-0437, 6_960 -> 5_080 lamports per byte,
#: in five separately gated steps). The quote path already makes one external
#: HTTP request for prices behind RATE_CACHE_SECONDS, so one RPC per ten
#: minutes is the same cost class as what is already there -- that is a
#: structural argument and NOT a measured latency: no Solana endpoint is
#: reachable from the environment this was written in, so nothing here has
#: timed a real call.
#:
#: IT IS NEVER A SUBSTITUTE FOR ASKING. An empty cache asks the cluster, and a
#: cluster that cannot be asked REFUSES the quote rather than falling back to a
#: compiled-in number -- which is the failure chains/solana_units.py's rent
#: section is a monument to.
RENT_FLOOR_CACHE_SECONDS = 600.0

#: {cache key: (monotonic deadline, lamports)}. Keyed by the endpoint so a
#: process pointed at a different cluster cannot reuse the first one's answer --
#: which is the whole hazard, since devnet and mainnet-beta have answered
#: different figures during a feature rollout.
_RENT_FLOOR_CACHE: dict[str, tuple[float, int]] = {}


def get_network_fee_reserve(config, to_asset: str) -> float:
    """What the desk EXPECTS one payout on this chain to cost it. Not charged to the customer.

    IT USED TO BE SUBTRACTED FROM THE PAYOUT, AND THAT WAS WRONG -- not by a
    little, and the justification for it was measurably false. See
    create_quote() below for the proof and the arithmetic; in short:

      * `sendtoaddress(address, amount)` delivers `amount` EXACTLY. The fee comes
        out of the wallet's own inputs, so nothing is taken from the payout.
        Measured on the operator's host 2026-10-01 to the last digit:
        `getreceivedbyaddress mmr6ATb3...` read 143.39622296 GRC against two
        output_amount_estimate values summing to 143.39622296 -- a difference of
        zero. Had the reserve funded the fee, the recipient would have been
        0.02 GRC short.
      * the real fee was 0.001 GRC, not 0.01. The payout transaction moved that
        wallet's balance by exactly -0.001, which is the fee and nothing else,
        because the payout address was its own.

    So the reserve was withheld from the customer, the wallet separately paid a
    tenth of it, and the remainder stayed as margin the schedule does not mention.

    IT IS STILL REQUIRED, and still refuses a pair that has none, for a reason that
    has changed. It is no longer "the chain will not deliver a payout quoted without
    it" -- the chain delivers fine. It is that a pair whose chain cost nobody has
    written down is a pair whose MARGIN is unknown: 150bps of a 56 GRC swap is
    0.85 GRC and a GRC payout costs 0.001, which is a fine trade, while the same
    150bps of a dust swap would not cover one transaction. The figure is booked as
    a cost and reported by show_fees.py; a missing one means nobody has checked.

    THE SUBSCRIPT USED TO BE BARE, AND IT REACHED THE OPERATOR AS A KEY'S REPR.
    2026-09-26, from their browser, after XRP<->GRC was added to ALLOWED_PAIRS:

        No quote: 'XRP_NETWORK_FEE_RESERVE'

    That is str(KeyError(...)). BTC, LTC and GRC each have a reserve in
    config.py; XRP was never given one when the pair was enabled, so the subscript
    raised, routes/quotes.py's HTTP boundary returned str(exc), and the page printed
    the key and nothing else -- the third bare KeyError repr to reach a person that
    day, after 'GRC' from create_swap() and the same shape one layer down.

    The reserve is NOT defaulted to zero here, and that is the point rather than an
    omission: zero would quote a payout with no allowance for the destination chain's
    fee, so the payout would be short by whatever the network charges, or fail. A
    reserve is a PRICING decision and belongs to the operator (rule 16), so a missing
    one refuses the quote and says which setting to add.
    """
    refusal = why_cannot_quote(config, to_asset)
    if refusal:
        # "Nothing was written." is appended HERE and is not part of the shared
        # reason, because it is true of an ATTEMPT and this is the only caller that
        # is one. services/pair_view.py asks the same question about a pair nobody
        # has picked yet, and telling that reader nothing was written would imply
        # something had been tried. Rule 14: say what the number means to the
        # reader who is actually there.
        raise ValueError(f"{refusal} Nothing was written.")
    return float(config[f"{to_asset}_NETWORK_FEE_RESERVE"])


def why_cannot_quote(config, to_asset: str) -> str:
    """Why a quote paying out in `to_asset` would refuse, or "" if it would price.

    THE SAME TEST AS get_network_fee_reserve()'s, ASKED WITHOUT RAISING, so a
    surface that lists pairs can show the refusal before a customer picks one.
    get_network_fee_reserve() is DERIVED from this rather than repeating the
    condition (rule 8); the raise is one line above.

    WHY THIS FUNCTION EXISTS, measured on the operator's own screen 2026-10-02.
    services/pair_view.pair_serviceability() evaluated THREE conditions -- an
    adapter for each chain, the destination can pay out, the source can take a
    deposit -- and GRC -> XRP passes all three on a host that has exported
    XRP_PAYOUT_SECRET_SEED. So the customer page rendered

        GRC -> XRP   AVAILABLE   Ready to quote now.
        2 of 13 directions can be quoted right now

    and the quote for it refused, from this function, for want of
    XRP_NETWORK_FEE_RESERVE. The page's own lede promises "the form below offers
    exactly the ones marked available, so what you see here and what you can pick
    cannot differ", and it differed.

    THE THREE CONDITIONS WERE NOT WRONG; THEY WERE INCOMPLETE. Being quotable is a
    FOURTH condition with its own authority, and the authority is this file --
    which is why the fix is a function here that pair_view calls, and not a
    `hasattr(Config, ...)` written a second time in pair_view. A second copy of
    this test is rule 8's shape, and the tree has already paid for exactly that
    on exactly this verdict: four implementations of the pay-out test, three of
    them wrong (see services/pair_view.py's header).

    PURE CONFIG, NO NETWORK AND NO ADAPTER, which is what lets a page call it for
    every pair on every render. It is also why it answers only about the
    DESTINATION: a reserve is keyed on the asset being paid out, and a source
    chain never sends.
    """
    key = f"{to_asset}_NETWORK_FEE_RESERVE"
    if key not in config:
        return (
            f"No quote: nobody has recorded what one {to_asset} payout costs this desk, so the margin on "
            f"a {to_asset} swap is unknown. Set {key} in the environment this process was started with -- "
            f"it is the expected chain fee for one payout, which the desk pays out of its own fee and "
            f"NOT out of the customer's payout. Defaulting it to zero would book a cost of nothing for a "
            f"transaction that is not free."
        )
    return ""


def new_account_floor_lamports(adapter, *, now=None) -> int:
    """The cluster's rent-exempt minimum for a 0-byte account, cached per endpoint.

    ASKED, NOT ASSUMED. SolanaAdapter.rent_exempt_minimum() calls
    getMinimumBalanceForRentExemption, and SYSTEM_ACCOUNT_SPACE is 0 because a
    plain wallet account holds no data -- chains/solana_units.py names both and
    records the operator's 2026-09-30 devnet reading of 650,240 lamports for
    that size. No figure is hardcoded here: the module that DID carry hardcoded
    reference values spent weeks saying 890,880 while every cluster answered
    650,240, and nothing failed while it did.

    `now` is a parameter so a test can expire the cache without sleeping.
    """
    moment = time.monotonic() if now is None else float(now)
    key = f"{getattr(adapter, 'url', '') or '(no url)'}|{getattr(adapter, 'mint', '') or 'native'}"
    cached = _RENT_FLOOR_CACHE.get(key)
    if cached and cached[0] > moment:
        return cached[1]
    lamports = int(adapter.rent_exempt_minimum(SYSTEM_ACCOUNT_SPACE))
    _RENT_FLOOR_CACHE[key] = (moment + RENT_FLOOR_CACHE_SECONDS, lamports)
    return lamports


def why_cannot_establish_payout_floor(adapters, to_asset: str) -> str:
    """Why a quote paying out in `to_asset` cannot even be PRICED, or "" if it can.

    THE FIFTH CONDITION, AND IT IS THE FIRST ONE THAT NEEDS A NETWORK. The four
    services/pair_view.pair_serviceability() already asks are answerable from config
    and the adapter table alone. This one is not, and pretending otherwise is what
    produced the failure that forced it:

        the customer page badges BTC -> SOL AVAILABLE -- Ready to quote now. -- and
        a real quote for it did not price. What the quote said: "this terminal
        cannot reach the Solana network right now, so it cannot establish the
        smallest payout the network will accept."

    Measured 2026-10-03, the moment the three *->SOL pairs were enabled. A SOL
    payout has a MINIMUM imposed by the runtime -- rent exemption for the account it
    would create -- and that figure comes from the cluster
    (getMinimumBalanceForRentExemption). A terminal that cannot ask cannot quote,
    and a page that cannot ask cannot honestly say AVAILABLE.

    SO THIS IS THE SAME SHAPE AS why_cannot_quote() ONE LEVEL OUT: the condition the
    quote enforces, asked without raising, so a surface can show it before a customer
    picks. And it goes through new_account_floor_lamports() -- the SAME function
    require_deliverable_sol_payout() calls, with the same 600-second per-endpoint
    cache -- so the page and the quote cannot disagree and the page does not add a
    cluster round trip per render (rule 8).

    ONLY SOL NEEDS IT TODAY and the function says so by returning "" for everything
    else rather than growing a table. BTC, LTC, GRC and XRP have no runtime-imposed
    payout minimum this desk must read from a chain; if one ever does, this is where
    it goes.

    A FAILURE TO ASK IS NOT A REFUSAL TO PAY, and the distinction decides what a
    customer is told: the chain being unreachable is a NOW problem (services/pair_view
    maps it to OFFLINE, "come back later"), while holding no signing key is not. The
    caller makes that distinction; this function only says the floor could not be
    established and why.
    """
    if to_asset != "SOL":
        return ""
    adapter = (adapters or {}).get("SOL")
    if adapter is None:
        return "SOL has no adapter in this process, so the smallest deliverable payout cannot be established"
    if getattr(adapter, "is_spl", False):
        return ""
    try:
        new_account_floor_lamports(adapter)
    except Exception as exc:  # noqa: BLE001 -- checked: the caller CAN tell this from a real answer, because the reason is returned and "" is the only value that means yes. Any cluster or transport failure is one answer here -- "we could not ask" -- and narrowing it would mean importing the adapter module's error type upward from a submodule.
        return (
            f"the Solana cluster could not be asked for the rent-exempt minimum, so the smallest "
            f"deliverable SOL payout is unknown: {type(exc).__name__}: {exc}"
        )
    return ""


def require_deliverable_sol_payout(config, adapters, to_asset: str, output_amount: float) -> None:
    """Refuse a SOL payout too small to create the account it would be sent to.

    OPERATOR INSTRUCTION, 2026-10-02: "refuse at quote too." The payout-time
    refusal (chains/solana_signing.require_destination_rent) was built first and
    was not enough on its own: by the time a payout runs, the customer's deposit
    has been taken and credited, so a refusal there leaves a swap in `failed`
    needing a person. Refused here, nothing has been taken and the customer gets
    a number they can act on.

    WHAT THIS CAN AND CANNOT ASK, established from the code rather than assumed.
    create_quote() takes (db, config, from_asset, to_asset, input_amount) and
    create_swap() takes the payout_address -- so AT QUOTE TIME THERE IS NO
    DESTINATION. "Does that account exist" is unanswerable here, and the only
    honest test is the conservative one: a payout below the floor cannot be
    delivered to a NEW account, whichever address arrives later. A payout ABOVE
    the floor is never refused here, so this cannot reject a swap that would
    have worked.

    The precise test -- "does THIS address exist, and if it does then any amount
    is deliverable" -- is answerable only once an address exists, and the place
    for it is services/swap_service.create_swap(), which has both the address
    and the adapters. IT IS NOT IMPLEMENTED THERE as of 2026-10-02 and that is
    named rather than left as a gap: this refusal is strictly conservative, so
    nothing that passes it is refused later for the same reason, and a customer
    never sees the round trip the operator's instruction exists to remove.

    NATIVE SOL ONLY. An SPL payout has a different and larger rent question --
    an associated token account is 165 bytes, its minimum was 1,488,440 on the
    operator's devnet run, and WHO pays it is a decision nobody has made -- so
    an SPL-configured adapter is skipped here rather than quoted against the
    wrong figure. chains/solana.py's payout path refuses an SPL send outright,
    so no quote this passes can become an SPL payout by accident.

    THE FEE RESERVE IS A DIFFERENT NUMBER AND IS NOT THIS ONE.
    SOL_NETWORK_FEE_RESERVE is what one payout costs the DESK (the 5,000-lamport
    signature fee), it is checked by services/swap_service.create_swap() against
    the desk's own fee, and it is not deducted from anybody's payout. This floor
    is a MINIMUM ON THE PAYOUT AMOUNT imposed by the runtime. Two numbers, two
    jobs; confusing them would either refuse a deliverable swap or accept an
    undeliverable one.
    """
    if to_asset != "SOL":
        return
    adapter = (adapters or {}).get("SOL")
    if adapter is None:
        raise ValueError(
            "No quote: this terminal cannot reach the Solana network right now, so it cannot establish "
            "the smallest payout the network will accept. Nothing was written. Try again shortly, or "
            "choose a different payout coin."
        )
    if getattr(adapter, "is_spl", False):
        return
    floor_lamports = new_account_floor_lamports(adapter)
    if below_new_account_floor(amount_to_base_units(output_amount, SOL_DECIMALS), floor_lamports):
        # CUSTOMER-FACING, AND IT NAMES NO INTERNAL SETTING. Commit 6ae5838
        # stripped operator-facing text off the customer page on 2026-10-02 for
        # exactly this reason, so this says what the limit is and what to do
        # about it, in SOL, and nothing about keypairs, clusters or config.
        #
        # decimal_amount() rather than an f-string float, because
        # chains/solana_pay.py already records what a float's repr does to a
        # small amount: 5e-05 is not a number a person can read off a screen
        # and type back.
        floor_sol = decimal_amount(floor_lamports / 10 ** SOL_DECIMALS)
        raise ValueError(
            f"No quote: a payout of {decimal_amount(output_amount)} SOL is too small to deliver. The "
            f"Solana network will not create a brand-new account holding less than {floor_sol} SOL, so a "
            f"payout under that cannot arrive at an address that has never been used. Deposit more and "
            f"the quote will work. Nothing was written."
        )


def validate_pair(config, from_asset: str, to_asset: str) -> None:
    if (from_asset, to_asset) not in config["ALLOWED_PAIRS"]:
        raise ValueError("Unsupported trading pair")


def _confidence_for_display(config, from_asset: str, to_asset: str, notional_usd: float) -> dict:
    """The market-depth reading for both legs, as plain data for a template.

    DISPLAY ONLY, AND IT CANNOT FAIL A QUOTE. Every failure returns a dict saying
    what could not be read, because a badge is not worth a refused swap: a quote
    whose numbers are all correct must not be lost to a diagnostic that could not
    be computed. That is the opposite of the pricing path's rule -- a missing
    PRICE refuses, a missing CONFIDENCE reports -- and the difference is that one
    decides an amount and the other decides a sentence.

    IT SHARES THE PRICE CACHE. fetch_market_context() reads the same
    _fetch_raw() the rate above already populated, so this costs no extra
    request inside the TTL window.
    """
    from .market_context import (  # noqa: PLC0415 -- checked: market_context imports pricing, and pricing is already imported at the top of this module; importing it at module scope here would make the import order of two services decide whether this one loads, which is the kind of coupling rule 12 warns about in its import-time note.
        QuoteWindow,
        price_confidence,
    )
    from .pricing import fetch_market_context  # noqa: PLC0415 -- same.

    # THE WHOLE BODY IS INSIDE THE TRY, AND IT WAS NOT UNTIL 2026-09-30. The docstring above
    # has always claimed this function "CANNOT FAIL A QUOTE" and that "every failure returns a
    # dict saying what could not be read" -- and the loop that builds the legs sat OUTSIDE the
    # except, so any failure in it propagated straight out of create_quote(). The guarantee was
    # documented and not implemented, which is the worst of the three states it could be in: a
    # reader checks the docstring, sees the promise, and stops looking.
    #
    # It was found by a defect it let through, on the line below: `finding.kind`, on a
    # dataclass whose fields are code/verdict/message. Every quote that reached a real snapshot
    # raised AttributeError -- the ORDER PATH, refusing every quote, for a badge. No test caught
    # it because the tests that exercise this produce an EMPTY findings list, so the
    # comprehension never evaluated its own body; and market_context.Finding's own docstring
    # records the same trap one field over ("three tests written that day asserted on docstring
    # and message TEXT and passed while the code was wrong").
    try:
        window = QuoteWindow(
            quote_ttl_seconds=float(config["QUOTE_TTL_SECONDS"]),
            rate_cache_seconds=float(config["RATE_CACHE_SECONDS"]),
            fee_bps=float(config["DEFAULT_FEE_BPS"]),
        )
        snapshots = {snapshot.asset: snapshot for snapshot in fetch_market_context(
            int(config["RATE_CACHE_SECONDS"]))}

        legs = {}
        for role, asset in (("from", from_asset), ("to", to_asset)):
            snapshot = snapshots.get(asset)
            if snapshot is None:
                legs[role] = {"asset": asset, "verdict": "UNKNOWN",
                              "reason": f"no market snapshot for {asset}"}
                continue
            # The notional is the same for both legs by construction: it is the swap's
            # size in dollars, and a size that is a large share of ONE side's daily
            # volume is the thing worth saying whichever side it is.
            reading = price_confidence(snapshot, window, swap_notional_usd=notional_usd)
            legs[role] = {
                "asset": asset,
                "verdict": reading.verdict,
                "reason": reading.reason,
                # `code`, not `kind`. Finding is (code, verdict, message) and its docstring
                # says why the split exists: "code is for assertions, message is for a human".
                "findings": [{"code": finding.code, "verdict": finding.verdict,
                              "message": finding.message} for finding in reading.findings],
            }
    except Exception as error:  # noqa: BLE001 -- checked: see the docstring, which this now actually implements. Every failure here costs a badge and nothing else, the reason is returned rather than swallowed, and no number in the quote depends on it. The catch is deliberately around the WHOLE body: the point is that no diagnostic in it can refuse a swap, and a try that covered only the setup was that promise made and not kept.
        return {"available": False, "why": f"{type(error).__name__}: {error}"}

    return {"available": True, "legs": legs}


def measured_or_configured_reserve(db, config, adapters, to_asset: str, payout_amount: float):
    """The reserve for THIS payout: measured off the chain where it can be, else the constant.

    (reserve, how). NEVER RAISES -- a failed measurement falls back to the
    configured figure and says so, because a quote that dies on a fee probe is
    worse than a quote priced on a constant.

    THE OPERATOR AUTHORIZED THIS 2026-10-03 after being shown the numbers, and the
    numbers are why a constant cannot be right. Measured on their node with
    fundrawtransaction, which selects real inputs from the real UTXO set:

        BTC   send 0.0004 -> fee 0.00002820      send 2701 -> fee 0.00084240   30x
        LTC   send 1.2    -> fee 0.00010372      send 2701 -> fee 0.00097643   9.4x

    A Bitcoin-style fee is bytes times a rate and the bytes are mostly INPUTS, so
    the figure moves by an order of magnitude across the payout sizes one desk
    makes. The configured constants were wrong in opposite directions on the same
    day -- BTC 41% low, LTC 4.7x high -- and no single value fixes both.

    GRC IS NOT SPECIAL-CASED, AND THAT IS DELIBERATE. Gridcoin is flat: 8 payouts
    at exactly 0.00100000, low equal to high, across amounts from 82 to 2701 GRC,
    which select different input counts. Its daemon also has no
    fundrawtransaction, so the probe returns "Method not found (rpc code -32601)"
    and this falls back to the constant -- which is the right answer for a flat
    chain, reached by ASKING rather than by keeping a second table of which chains
    scale (rule 8). A chain that gains the RPC starts being measured with no
    change here; one that loses it starts falling back.

    WHY THE ADDRESS IS A PAST DEPOSIT ADDRESS OF OUR OWN, and this is the
    assumption the operator asked be stated rather than hidden:

      * The fee depends on the INPUTS the wallet selects and on the output's TYPE,
        not on which address of that type it pays. A p2wpkh output is 31 bytes
        whoever owns it.
      * The payout's real destination is the CUSTOMER's address and is not known
        at quote time -- create_quote() runs before create_swap() receives it. So
        some address has to stand in.
      * It must not be derived here: getnewaddress would answer in one call and is
        a WALLET WRITE, and a quote must not leave a key behind in the hot wallet.
      * swaps.deposit_address rows for this asset are addresses THIS wallet
        derived for its own deposits, so they are ours, on the right chain, and
        already on disk.

      THE RESIDUAL ERROR IS THE OUTPUT TYPE. If the customer pays to p2pkh (34
      bytes) where the stand-in is p2wpkh (31), the estimate is ~3 bytes light --
      about 3 satoshis at a 1 sat/byte rate, against an input-count effect of
      30x. Named here because it is a real approximation, not because it matters
      at that size.

    NO ADDRESS ON FILE MEANS THE CONSTANT, which is the state of every chain this
    desk has never taken a deposit on. It is not an error: the constant is what
    was used before this function existed.
    """
    configured = get_network_fee_reserve(config, to_asset)
    adapter = (adapters or {}).get(to_asset)
    if adapter is None or not hasattr(adapter, "measure_send_fee"):
        return configured, (f"the configured {to_asset}_NETWORK_FEE_RESERVE, because no {to_asset} "
                            f"adapter in this process can measure a send")
    if payout_amount <= 0:
        return configured, (f"the configured {to_asset}_NETWORK_FEE_RESERVE, because a payout of "
                            f"{payout_amount!r} has no fee to measure")
    address = own_address_on_chain(db, to_asset)
    if not address:
        return configured, (f"the configured {to_asset}_NETWORK_FEE_RESERVE, because this desk holds no "
                            f"{to_asset} address of its own to measure a send against -- no swap has "
                            f"ever taken a {to_asset} deposit")
    fee, how = adapter.measure_send_fee(address, payout_amount)
    if fee is None:
        return configured, (f"the configured {to_asset}_NETWORK_FEE_RESERVE, because the chain could not "
                            f"be asked: {how}")
    return fee, (f"MEASURED off the {to_asset} chain for this payout's size ({how}), against a "
                 f"configured {to_asset}_NETWORK_FEE_RESERVE of {configured}")


def own_address_on_chain(db, asset: str) -> str:
    """One address on `asset` that this desk derived for itself, or "".

    READS swaps.deposit_address, which services/swap_service.deposit_account()
    wrote by asking this wallet for a fresh address -- so every row is ours and on
    the right chain. Newest first, because an older one may belong to a wallet the
    operator has since repointed, and a stand-in for a fee measurement only has to
    be a valid address of the right TYPE.

    IT EXISTS SO THE QUOTE PATH DOES NOT CALL getnewaddress. That is a wallet
    write -- it derives and stores a key -- and a priced quote that leaves a key
    behind would put one in the wallet for every page refresh.
    """
    row = db.execute(
        "SELECT deposit_address FROM swaps WHERE from_asset = ? AND deposit_address IS NOT NULL "
        "AND deposit_address != '' ORDER BY created_at DESC LIMIT 1",
        (asset,),
    ).fetchone()
    return str(row["deposit_address"]) if row else ""


def create_quote(db, config, from_asset: str, to_asset: str, input_amount: float, *, adapters=None) -> dict:  # noqa: PLR0913 -- checked: the first five ARE the quote (where to write it, the settings, the pair, the size) and the sixth is the only object that can ask a chain a question. Bundling them would add a type without removing a parameter. It is KEYWORD-ONLY, which is the same property the arming token on the payout path relies on: a positional argument that drifted one place cannot land in it. Same judgment recorded at chains/xrp.py:773 and chains/solana.py's __init__.
    from_asset = from_asset.upper().strip()
    to_asset = to_asset.upper().strip()
    input_amount = float(input_amount)
    if input_amount <= 0:
        raise ValueError("Input amount must be positive")
    validate_pair(config, from_asset, to_asset)
    prices = fetch_usd_prices(config["RATE_CACHE_SECONDS"])
    rate = derive_pair_rate(from_asset, to_asset, prices)
    fee_bps = int(config["DEFAULT_FEE_BPS"])
    # THE RESERVE IS RESOLVED AFTER THE PAYOUT IS KNOWN, because since 2026-10-03
    # it may be MEASURED for this payout's size and the fee depends on that size.
    # There is no circularity: the reserve is no longer subtracted from the payout
    # (see the long note below), so the payout is a function of the rate and the
    # fee alone and can be computed first.
    gross_output = input_amount * rate
    # THE RESERVE IS NOT SUBTRACTED, and it was until 2026-10-02. The operator's
    # instruction was "fix the regressive reserve", and what made it regressive is
    # that a FLAT amount withheld from a percentage fee is a bigger share of a
    # small payout than of a large one. Measured across their six delivered
    # payouts, the reserve's contribution to the realized fee ran
    #
    #     56 GRC gross    1.8bps        89 GRC gross    1.1bps
    #     84 GRC gross    1.2bps      3134 GRC gross    0.0bps
    #
    # a sixty-fold spread in what a customer paid, decided by nothing but the size
    # of their swap, on top of a schedule that says 150bps flat.
    #
    # AND IT WAS NOT PAYING FOR ANYTHING. `sendtoaddress(address, amount)` delivers
    # `amount` exactly and takes its fee from the wallet's own inputs, so the
    # withheld amount never reached a miner. Measured to the last digit on the
    # operator's host 2026-10-01: `getreceivedbyaddress mmr6ATb3...` read
    # 143.39622296 GRC against two output_amount_estimate values summing to
    # 143.39622296 -- difference zero. Had the reserve funded the fee, the
    # recipient would have been 0.02 GRC short across those two payouts. The real
    # fee was 0.001 GRC, a tenth of the reserve, and the wallet paid it separately:
    # the payout transaction moved that wallet's balance by exactly -0.001, because
    # the payout address was its own.
    #
    # So the customer now pays fee_bps and nothing else, at every size, and the
    # chain fee is a cost the desk carries out of that fee -- which is what a
    # quoted percentage means everywhere else. get_network_fee_reserve() still
    # supplies the figure, it is still stored per swap, and show_fees.py reports it
    # as a COST rather than as part of what was charged.
    #
    # max(..., 0.0) stays: a fee fraction at or above 1 would otherwise quote a
    # negative payout, and the clamp is what services/swap_service.py's
    # below-minimum refusal reads.
    output_amount_estimate = max(gross_output * (1 - fee_bps / 10000.0), 0.0)
    # NOW the reserve, with the payout size in hand. measured_or_configured_reserve()
    # says which source it used and why, and that sentence reaches the log rather
    # than only the comment: the figure it returns is stamped on the quote row and
    # decides both the fee floor at create_swap() and the funding gate, so a reader
    # reconciling a refusal has to be able to find out whether it came off the
    # chain or out of config.
    network_fee_reserve, reserve_source = measured_or_configured_reserve(
        db, config, adapters, to_asset, output_amount_estimate
    )
    logger.info("quote reserve for %s->%s: %s %s  <- %s",
                from_asset, to_asset, network_fee_reserve, to_asset, reserve_source)
    # AND A SOL PAYOUT HAS A FLOOR THE NETWORK IMPOSES, checked HERE -- after
    # the payout figure exists and BEFORE the quote row is written, so a refusal
    # leaves nothing behind. See require_deliverable_sol_payout() for what it
    # can ask at this stage and what it deliberately cannot.
    #
    # `adapters` IS OPTIONAL IN THE SIGNATURE AND ALL THREE REAL CALLERS PASS IT:
    # routes/quotes.py passes current_app.config["ADAPTERS"], open_swap.py passes
    # the dict it already built, and operator_panel.answer_a_teller_quote()
    # passes the one _teller_db_and_config() returns. THIS COMMENT SAID "BOTH
    # REAL CALLERS" AND COUNTED TWO, 2026-10-02: the third was discarding its
    # adapters into `_adapters` and calling without them, so the teller pane
    # would have refused a *->SOL quote the web form accepts -- two spellings of
    # one answer, which is rule 8's defect with a delay on it. Counted by
    # `grep -rn "create_quote(" --include=*.py`, not recalled. It is optional
    # because tests and future
    # callers that quote a pair with no chain adapter exist, and because a
    # required argument would have been a breaking change to a function whose
    # three callers are in three different layers -- so the default is None and
    # a SOL-destined quote with no SOL adapter REFUSES rather than skipping the
    # check (rule 19: a gate that can be silently bypassed is not a gate).
    #
    # IT CANNOT FIRE TODAY, and that is said out loud rather than discovered:
    # Config.ALLOWED_PAIRS names no pair whose TO asset is SOL (counted
    # 2026-10-02 -- SOL appears only as a source), so validate_pair() above
    # refuses every *->SOL quote before this line is reached. This is the gate
    # for the moment the operator enables one, which is exactly when nobody
    # would remember to add it.
    require_deliverable_sol_payout(config, adapters, to_asset, output_amount_estimate)
    now = utc_now()
    quote = {
        "id": new_id("q"),
        "from_asset": from_asset,
        "to_asset": to_asset,
        "input_amount": input_amount,
        "quoted_rate": rate,
        "fee_bps": fee_bps,
        "network_fee_reserve": network_fee_reserve,
        "output_amount_estimate": output_amount_estimate,
        "expires_at": (now + timedelta(seconds=int(config["QUOTE_TTL_SECONDS"]))).isoformat(),
        "created_at": now.isoformat(),
    }
    # PROVENANCE AND CONFIDENCE, ADDED TO THE RESPONSE ONLY -- no number above
    # changes, and that is the whole boundary.
    #
    # services/market_context.price_confidence() names four ways it could be
    # wired in and marks three of them as fund movement: REFUSE on a verdict,
    # WIDEN the fee, CAP the size. The fourth is "BADGE the quote -- returning
    # the verdict alongside the quote for the page to display, changing no
    # number", and it is the only one authorized (operator, 2026-09-30, asked
    # for the diagnostics on the page). quoted_rate, fee_bps,
    # network_fee_reserve and output_amount_estimate are computed exactly as
    # before and are not touched below.
    #
    # THE PRICE SOURCE IS NOT PERSISTED, and the quotes table has no column for
    # it. That is a gap rather than a decision: which feed priced a quote is
    # provenance the next reader wants, and storing it needs a migration. Named
    # here so it is a known follow-up rather than an oversight.
    quote["price_source"] = last_price_source()
    quote["price_fetched_at"] = prices.get("fetched_at")
    quote["confidence"] = _confidence_for_display(config, from_asset, to_asset, gross_output)
    db.execute(
        """
        INSERT INTO quotes (
            id, from_asset, to_asset, input_amount, quoted_rate, fee_bps,
            network_fee_reserve, output_amount_estimate, expires_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            quote["id"], quote["from_asset"], quote["to_asset"], quote["input_amount"],
            quote["quoted_rate"], quote["fee_bps"], quote["network_fee_reserve"],
            quote["output_amount_estimate"], quote["expires_at"], quote["created_at"],
        ),
    )
    db.commit()
    return quote
