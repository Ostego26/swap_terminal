#!/usr/bin/env python3
"""
File: market_data.py

Description:
  This module provides functions to fetch market prices from CoinGecko with caching.
  The cache duration is set to 180 seconds (3 minutes) by default. A caller may force a refresh.
  
  The module supports:
    - Fetching BTC, LTC, GRC, and BTS prices (in USD).
    - Includes a Market class to interface with BitShares DEX for trading.

"""

import requests
import time
import logging
from typing import Union, Dict
from bitshares import BitShares
from bitshares.account import Account

# Configure logger for debugging
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# Global cache variables for BTC and LTC prices.
_cached_btc_ltc: Union[Dict[str, Union[float, str]], None] = None
_btc_ltc_timestamp: float = 0.0
_btc_ltc_cache_duration: float = 180  # seconds

# Global cache variables for GRC price.
_cached_grc: Union[float, str, None] = None
_grc_timestamp: float = 0.0
_grc_cache_duration: float = 180  # seconds

# Global cache variables for BTS price.
_cached_bts: Union[float, str, None] = None
_bts_timestamp: float = 0.0
_bts_cache_duration: float = 180  # seconds


class Market:
    """
    Represents a market on the BitShares DEX.
    """
    def __init__(self, market_name: str, bitshares_instance: BitShares):
        self.market_name = market_name
        self.bitshares_instance = bitshares_instance

    def ticker(self) -> Dict[str, float]:
        """
        Fetch the latest ticker data for the market.
        
        Returns:
            dict: Ticker information with the latest price.
        """
        try:
            ticker_data = self.bitshares_instance.get_ticker(self.market_name)
            return ticker_data
        except Exception as e:
            logger.error(f"Error fetching ticker for market {self.market_name}: {e}")
            return {"latest": 0.0}

    def buy(self, amount: float, price: float, account_name: str):
        """
        Place a buy order on the market.
        
        Args:
            amount (float): Amount of the base asset to buy.
            price (float): Price per unit of the base asset.
            account_name (str): The account placing the order.
        """
        try:
            self.bitshares_instance.market_order(account_name, 'buy', self.market_name, amount, price)
            logger.info(f"Placed buy order for {amount} at {price} on {self.market_name}.")
        except Exception as e:
            logger.error(f"Error placing buy order: {e}")
            raise

    def sell(self, amount: float, price: float, account_name: str):
        """
        Place a sell order on the market.
        
        Args:
            amount (float): Amount of the base asset to sell.
            price (float): Price per unit of the base asset.
            account_name (str): The account placing the order.
        """
        try:
            self.bitshares_instance.market_order(account_name, 'sell', self.market_name, amount, price)
            logger.info(f"Placed sell order for {amount} at {price} on {self.market_name}.")
        except Exception as e:
            logger.error(f"Error placing sell order: {e}")
            raise


def fetch_btc_ltc_prices(force_refresh: bool = False) -> Dict[str, Union[float, str]]:
    """
    Fetch the current BTC and LTC prices (in USD) from CoinGecko.
    
    The results are cached for _btc_ltc_cache_duration seconds unless force_refresh is True.
    
    Expected JSON response:
      {
         "bitcoin": {"usd": <price>},
         "litecoin": {"usd": <price>}
      }
    
    Args:
        force_refresh (bool): If True, bypass the cache and fetch fresh data.
    
    Returns:
        Dict[str, Union[float, str]]: A dictionary with keys "BTC" and "LTC" containing their USD prices.
                                     If an error occurs, values will be "N/A".
    """
    global _cached_btc_ltc, _btc_ltc_timestamp
    now = time.time()
    if (not force_refresh and _cached_btc_ltc is not None and 
            (now - _btc_ltc_timestamp) < _btc_ltc_cache_duration):
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
    except Exception as e:
        logger.error(f"Error fetching BTC and LTC prices: {e}")
        return {"BTC": "N/A", "LTC": "N/A"}


def fetch_grc_price(force_refresh: bool = False) -> Union[float, str]:
    """
    Fetch the current Gridcoin (GRC) price (in USD) from CoinGecko.
    
    The result is cached for _grc_cache_duration seconds unless force_refresh is True.
    
    Expected JSON response:
      {"gridcoin-research": {"usd": <price>}}
    
    Args:
        force_refresh (bool): If True, bypass the cache and fetch fresh data.
    
    Returns:
        Union[float, str]: The current GRC price in USD, or "N/A" if fetching fails.
    """
    global _cached_grc, _grc_timestamp
    now = time.time()
    if not force_refresh and _cached_grc is not None and (now - _grc_timestamp) < _grc_cache_duration:
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
    except Exception as e:
        logger.error(f"Error fetching GRC price from CoinGecko: {e}")
        return "N/A"


def fetch_bts_price(force_refresh: bool = False) -> Union[float, str]:
    """
    Fetch the current BTS price (in USD) from CoinGecko or other source.
    """
    global _cached_bts, _bts_timestamp
    now = time.time()
    if not force_refresh and _cached_bts is not None and (now - _bts_timestamp) < _bts_cache_duration:
        return _cached_bts

    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=bitshares&vs_currencies=usd"
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()
        bts_price = data.get("bitshares", {}).get("usd")
        if bts_price is None:
            raise ValueError("Missing BTS price in response.")
        _cached_bts = bts_price
        _bts_timestamp = now
        return bts_price
    except Exception as e:
        logger.error(f"Error fetching BTS price from CoinGecko: {e}")
        return "N/A"
