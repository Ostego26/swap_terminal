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

from datetime import timedelta

from .helpers import new_id, utc_now
from .pricing import derive_pair_rate, fetch_usd_prices, last_price_source


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
    key = f"{to_asset}_NETWORK_FEE_RESERVE"
    if key not in config:
        raise ValueError(
            f"No quote: nobody has recorded what one {to_asset} payout costs this desk, so the margin on "
            f"a {to_asset} swap is unknown. Set {key} in the environment this process was started with -- "
            f"it is the expected chain fee for one payout, which the desk pays out of its own fee and "
            f"NOT out of the customer's payout. Defaulting it to zero would book a cost of nothing for a "
            f"transaction that is not free. Nothing was written."
        )
    return float(config[key])


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


def create_quote(db, config, from_asset: str, to_asset: str, input_amount: float) -> dict:
    from_asset = from_asset.upper().strip()
    to_asset = to_asset.upper().strip()
    input_amount = float(input_amount)
    if input_amount <= 0:
        raise ValueError("Input amount must be positive")
    validate_pair(config, from_asset, to_asset)
    prices = fetch_usd_prices(config["RATE_CACHE_SECONDS"])
    rate = derive_pair_rate(from_asset, to_asset, prices)
    fee_bps = int(config["DEFAULT_FEE_BPS"])
    network_fee_reserve = get_network_fee_reserve(config, to_asset)
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
