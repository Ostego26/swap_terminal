from datetime import timedelta
from .helpers import new_id, utc_now, utc_now_iso
from .pricing import fetch_usd_prices, derive_pair_rate


def get_network_fee_reserve(config, to_asset: str) -> float:
    return float(config[f"{to_asset}_NETWORK_FEE_RESERVE"])


def validate_pair(config, from_asset: str, to_asset: str) -> None:
    if (from_asset, to_asset) not in config["ALLOWED_PAIRS"]:
        raise ValueError("Unsupported trading pair")


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
