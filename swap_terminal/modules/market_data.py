#!/usr/bin/env python3
"""CoinGecko spot prices for BTC, LTC and GRC, with a process-wide cache.

Role: submodule (price source for the atomic-swap suite)
Reads: https://api.coingecko.com/api/v3/simple/price
Writes: an in-process cache only
Can move funds: no -- but modules/atomic_swapper.py multiplies these numbers by
       the swap amount to compute what the counterparty is expected to send, so
       a wrong price here becomes a wrong expectation about somebody else's
       leg. It is an input to a fund-moving decision, not the decision.
Mainnet-safe: yes; it talks to a price API, not to a chain.

WHAT WAS REMOVED FROM THIS FILE ON 2026-09-24, AND WHY IT MATTERED.

This module used to open with

    from bitshares import BitShares
    from bitshares.account import Account

and carry a `Market` class wrapping the BitShares DEX, plus a
`fetch_bts_price()` and its cache globals. All of it was dead: the only
consumer of `Market` was modules/bitshares_client.py, and that file had ZERO
consumers anywhere in the tree -- established by grepping the whole tree for
the NAME, not just the import graph (rule 2). `fetch_bts_price` had no
consumers at all.

Removing it is not only tidiness, and this is the measurable part (rule 3).
`bitshares` is not in requirements.txt, which lists exactly two packages, so
the import at the top of this file made the module unimportable in a clean
checkout:

    before:  import modules.market_data -> ModuleNotFoundError: No module named 'bitshares'
    after:   import modules.market_data -> OK

and since modules/atomic_swapper.py imports this module, the entire
atomic-swap path was unimportable from a correct install of the declared
requirements. A dead class took a live path down with it, which is rule 9's
argument in one line: dead code is not inert.

THE BROAD EXCEPTS HERE RETURN "N/A", AND THAT IS ANNOTATED RATHER THAN LEFT
IMPLICIT. See the note at each site: a string is not a price, and the point of
returning one is that no caller can mistake it for a number.
"""

import logging
import time

import requests

# Configure logger for debugging. Deliberately NOT setLevel(DEBUG) with a
# StreamHandler attached, which forces DEBUG output on every program that
# imports the module and gives it no way to turn it back off. A library module
# lets the application decide.
#
# THIS COMMENT USED TO SAY "several modules in this package do that at import
# time", naming the practice as current. As of 2026-09-25 NONE do: the last
# three were atomic_btc_client, atomic_ltc_client and atomic_grc_client, and
# what they were forcing to DEBUG turned out to be a line that printed every
# RPC payload -- including the raw transaction whose scriptSig carries an HTLC
# preimage. See describe_rpc_payload() in modules/htlc_rpc.py. This file was
# right about the hazard before anybody measured it; the sentence is updated
# because it now describes a practice that is gone, and a comment that names a
# defect as present is the same bug pointed the other way (rule 16).
logger = logging.getLogger(__name__)

# Cache duration in SECONDS. It is compared against time.time() arithmetic, so
# it stays in seconds (rule 6: convert on the way out, at the print, not on the
# way in to control flow).
_CACHE_DURATION_SECONDS: float = 180

# Global cache for BTC and LTC prices.
_cached_btc_ltc: dict[str, float | str] | None = None
_btc_ltc_timestamp: float = 0.0

# Global cache for the GRC price.
_cached_grc: float | str | None = None
_grc_timestamp: float = 0.0


def fetch_btc_ltc_prices(force_refresh: bool = False) -> dict[str, float | str]:
    """Fetch the current BTC and LTC prices in USD from CoinGecko.

    Cached for _CACHE_DURATION_SECONDS unless force_refresh is True.

    Returns:
        {"BTC": <price>, "LTC": <price>} on success, or {"BTC": "N/A",
        "LTC": "N/A"} if the fetch failed.

    The failure value is a STRING, on purpose, and that is what makes the broad
    except at the bottom legitimate under rule 12. The rule's test is whether
    the caller can tell a failure from a real answer: 0.0 would be
    indistinguishable from a real price of zero and would silently produce a
    swap expectation of nothing, while "N/A" cannot be multiplied or compared
    numerically -- modules/atomic_swapper.py's `Decimal(str(...))` raises
    InvalidOperation on it rather than proceeding. The failure announces itself
    at the first attempt to use it.
    """
    global _cached_btc_ltc, _btc_ltc_timestamp  # noqa: PLW0603 -- module-level memo cache; the alternative is a class or a mutable dict, and neither is worth the churn in a file this size
    now = time.time()
    if not force_refresh and _cached_btc_ltc is not None and (now - _btc_ltc_timestamp) < _CACHE_DURATION_SECONDS:
        return _cached_btc_ltc

    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,litecoin&vs_currencies=usd"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        btc_price = data.get("bitcoin", {}).get("usd")
        ltc_price = data.get("litecoin", {}).get("usd")
        if btc_price is None or ltc_price is None:
            raise ValueError("Missing BTC or LTC price in response.")
        _cached_btc_ltc = {"BTC": btc_price, "LTC": ltc_price}
        _btc_ltc_timestamp = now
        return _cached_btc_ltc
    except Exception as e:  # noqa: BLE001 -- checked: network, HTTP, JSON and shape failures all mean "no price", and the return value says so in a form no caller can use as a number. See the docstring.
        logger.error(f"Error fetching BTC and LTC prices: {e}")
        return {"BTC": "N/A", "LTC": "N/A"}


def fetch_grc_price(force_refresh: bool = False) -> float | str:
    """Fetch the current Gridcoin price in USD from CoinGecko.

    Cached for _CACHE_DURATION_SECONDS unless force_refresh is True. Returns
    "N/A" on failure, for the reason given in fetch_btc_ltc_prices' docstring.
    """
    global _cached_grc, _grc_timestamp  # noqa: PLW0603 -- module-level memo cache; see fetch_btc_ltc_prices
    now = time.time()
    if not force_refresh and _cached_grc is not None and (now - _grc_timestamp) < _CACHE_DURATION_SECONDS:
        return _cached_grc

    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=gridcoin-research&vs_currencies=usd"
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()
        grc_price = data.get("gridcoin-research", {}).get("usd")
        if grc_price is None:
            raise ValueError("Missing GRC price in response.")
        _cached_grc = grc_price
        _grc_timestamp = now
        return grc_price
    except Exception as e:  # noqa: BLE001 -- checked: same reasoning as fetch_btc_ltc_prices; "N/A" is not usable as a number by any caller
        logger.error(f"Error fetching GRC price from CoinGecko: {e}")
        return "N/A"
