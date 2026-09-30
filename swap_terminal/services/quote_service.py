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
    """What is held back from the payout for the destination chain's own fee.

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
            f"No quote: {to_asset} has no network fee reserve, so a payout amount cannot be computed. "
            f"Set {key} in the environment this process was started with -- it is the amount held back "
            f"from every {to_asset} payout for that chain's own transaction fee, and defaulting it to "
            f"zero would quote a payout the chain will not deliver. Nothing was written."
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

    try:
        window = QuoteWindow(
            quote_ttl_seconds=float(config["QUOTE_TTL_SECONDS"]),
            rate_cache_seconds=float(config["RATE_CACHE_SECONDS"]),
            fee_bps=float(config["DEFAULT_FEE_BPS"]),
        )
        snapshots = {snapshot.asset: snapshot for snapshot in fetch_market_context(
            int(config["RATE_CACHE_SECONDS"]))}
    except Exception as error:  # noqa: BLE001 -- checked: see the docstring. Every failure here costs a badge and nothing else, the reason is returned rather than swallowed, and no number in the quote depends on it.
        return {"available": False, "why": f"{type(error).__name__}: {error}"}

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
            "findings": [{"kind": finding.kind, "verdict": finding.verdict,
                          "message": finding.message} for finding in reading.findings],
        }
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
    output_amount_estimate = max(gross_output * (1 - fee_bps / 10000.0) - network_fee_reserve, 0.0)
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
