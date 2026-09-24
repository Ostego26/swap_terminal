import logging
import os
from typing import Tuple

# Import Market at the top to avoid issues
from modules.market_data import Market  # Correct import for Market

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class BitSharesClient:
    def __init__(self) -> None:
        """
        Initialize the BitShares client with environment variables for:
        - BTS_NODE_URL   (default: wss://api.testnet.bitshares.ws)
        - BTS_WIF_KEY    (default: empty)
        - BTS_ACCOUNT    (default: my-bitshares-account)
        """
        self.node_url = os.environ.get("BTS_NODE_URL", "wss://api.testnet.bitshares.ws")
        
        # Lazy import to avoid circular import issue
        from bitshares import BitShares
        from bitshares.account import Account

        # Initialize BitShares and Account after import
        self.bitshares = BitShares(
            self.node_url,
            keys=[os.environ.get("BTS_WIF_KEY", "")],
            blocking=True
        )
        self.account_name = os.environ.get("BTS_ACCOUNT", "my-bitshares-account")
        self.account = Account(self.account_name, bitshares_instance=self.bitshares)

    def _fetch_latest_price(self, market: 'Market') -> float:
        """
        Helper method to fetch the latest 'settle_price' from a market.
        Raises RuntimeError if fetching fails.
        """
        try:
            ticker = market.ticker()
            return ticker.get("latest", 1.0) or 1.0
        except Exception as exc:
            raise RuntimeError(f"Failed to fetch ticker for market {market.symbol}: {exc}")

    def market_swap(
        self,
        base_symbol: str,
        quote_symbol: str,
        amount_in: float,
        is_buy: bool = True
    ) -> Tuple[float, float]:
        """
        Perform a buy or sell on the BitShares DEX.
        
        :param base_symbol:  Symbol of the base asset (e.g., "BTS").
        :param quote_symbol: Symbol of the quote asset (e.g., "USD").
        :param amount_in:    How much asset to trade:
                             - If is_buy=True, this is the amount of quote you spend.
                             - If is_buy=False, this is the amount of base you sell.
        :param is_buy:       True to buy base with quote, False to sell base for quote.

        :return: A tuple (base_amount, quote_amount):
                 - If is_buy=True: (base_bought, quote_spent)
                 - If is_buy=False: (base_sold, quote_acquired)

        :raises RuntimeError: for insufficient funds/fees or any other unexpected error.
        """
        market_name = f"{base_symbol}:{quote_symbol}"
        market = Market(market_name, bitshares_instance=self.bitshares)

        # 1) Get the latest price
        settle_price = self._fetch_latest_price(market)

        # 2) Place the order
        try:
            if is_buy:
                # Buying base => spending quote
                amount_base = amount_in / settle_price
                limit_price = settle_price * 10.0  # a high limit to ensure fill
                market.buy(
                    amount=amount_base,
                    price=limit_price,
                    account=self.account_name
                )
                return (amount_base, amount_in)
            else:
                # Selling base => receiving quote
                limit_price = settle_price * 0.1  # a low limit to ensure fill
                market.sell(
                    amount=amount_in,
                    price=limit_price,
                    account=self.account_name
                )
                acquired_quote = amount_in * settle_price
                return (amount_in, acquired_quote)

        except Exception as exc:
            # bitshares==0.7.1 does not have InsufficientFeeException, so we parse error text
            msg = str(exc)
            if "insufficient_fee" in msg or "Insufficient Fee" in msg:
                raise RuntimeError(
                    "BitShares account has insufficient fee to place this order."
                ) from exc
            if "insufficient_balance" in msg:
                raise RuntimeError(
                    f"BitShares account has insufficient balance to place this order: {exc}"
                ) from exc
            # Otherwise, a generic error
            raise RuntimeError(f"BitShares market swap failed: {exc}")
