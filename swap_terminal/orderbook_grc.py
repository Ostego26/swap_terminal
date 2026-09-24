import time
import uuid
import hashlib
import secrets
import logging
import requests  # For external API calls

# Configure logging to display timestamp and level information
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# --- External Pricing Functions ---

# Mapping asset symbols to CoinGecko IDs.
COINGECKO_ID_MAP = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "LTC": "litecoin",
    "GRC": "gridcoin-research"  # Live price for Gridcoin will be fetched from CoinGecko.
}

def get_external_price(asset, vs_currency="usd"):
    """
    Retrieve the current exchange rate for a given asset (e.g., BTC, ETH, GRC) in terms of vs_currency.
    This function fetches live data from CoinGecko.
    """
    asset = asset.upper()
    coin_gecko_id = COINGECKO_ID_MAP.get(asset)
    if not coin_gecko_id:
        logging.error(f"No CoinGecko mapping for asset {asset}")
        return None

    # Adding a cache buster to avoid potential caching issues.
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_gecko_id}&vs_currencies={vs_currency.lower()}&cache_buster={int(time.time())}"
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        # Log the full JSON response for debugging.
        logging.info(f"[CoinGecko] Raw JSON response for {asset}: {data}")
        price = data[coin_gecko_id][vs_currency.lower()]
        logging.info(f"[CoinGecko] {asset} price in {vs_currency.upper()}: {price}")
        return price
    except Exception as e:
        logging.error(f"Error fetching price from CoinGecko for {asset}: {e}")
        return None

# --- Order, Trade, and HTLC Classes ---

class Order:
    def __init__(self, order_type, asset, amount, price, user_id):
        self.order_id = str(uuid.uuid4())
        self.order_type = order_type.lower()  # 'buy' or 'sell'
        self.asset = asset  # e.g., 'BTC', 'LTC', or 'GRC'
        self.amount = amount  # quantity to trade (may be partially filled)
        self.price = price  # price per unit
        self.user_id = user_id
        self.timestamp = time.time()
        self.status = 'open'  # other statuses: 'partial', 'filled'

    def __repr__(self):
        return (f"Order(id={self.order_id[:8]}, type={self.order_type}, asset={self.asset}, "
                f"amount={self.amount}, price={self.price}, user={self.user_id}, "
                f"time={self.timestamp:.2f}, status={self.status})")


class Trade:
    def __init__(self, buy_order, sell_order, amount, price):
        self.trade_id = str(uuid.uuid4())
        self.buy_order_id = buy_order.order_id
        self.sell_order_id = sell_order.order_id
        self.asset = buy_order.asset
        self.amount = amount
        self.price = price
        self.timestamp = time.time()
        self.htlc = None  # Will be assigned after HTLC creation

    def __repr__(self):
        return (f"Trade(id={self.trade_id[:8]}, asset={self.asset}, amount={self.amount}, "
                f"price={self.price}, buy_order={self.buy_order_id[:8]}, "
                f"sell_order={self.sell_order_id[:8]}, time={self.timestamp:.2f})")


class HTLCContract:
    def __init__(self, buyer, seller, trade, locktime=3600):
        # Generate a random secret and compute its SHA256 hash.
        self.secret = secrets.token_hex(16)
        self.secret_hash = hashlib.sha256(self.secret.encode()).hexdigest()
        self.buyer = buyer
        self.seller = seller
        self.trade = trade
        self.locktime = locktime  # Seconds until expiration
        self.created_at = time.time()
        self.settled = False

    def is_expired(self):
        return time.time() > self.created_at + self.locktime

    def settle(self, revealed_secret):
        """Simulate settling the HTLC contract by verifying the revealed secret."""
        if hashlib.sha256(revealed_secret.encode()).hexdigest() == self.secret_hash:
            self.settled = True
            logging.info(f"HTLC settled for trade {self.trade.trade_id[:8]} using secret {revealed_secret}")
            return True
        else:
            logging.error("Failed to settle HTLC: invalid secret")
            return False

    def __repr__(self):
        return (f"HTLC(buyer={self.buyer}, seller={self.seller}, trade_id={self.trade.trade_id[:8]}, "
                f"secret_hash={self.secret_hash[:8]}, locktime={self.locktime}, "
                f"created_at={self.created_at:.2f}, settled={self.settled})")


# --- Order Book and Engine ---

class OrderBook:
    def __init__(self):
        # For simplicity we maintain one list for buys and one for sells.
        self.buy_orders = []   # Sorted descending by price, then by timestamp
        self.sell_orders = []  # Sorted ascending by price, then by timestamp

    def add_order(self, order):
        if order.order_type == 'buy':
            self.buy_orders.append(order)
            self.buy_orders.sort(key=lambda o: (-o.price, o.timestamp))
        elif order.order_type == 'sell':
            self.sell_orders.append(order)
            self.sell_orders.sort(key=lambda o: (o.price, o.timestamp))
        else:
            raise ValueError("order_type must be 'buy' or 'sell'.")

    def remove_order(self, order):
        if order.order_type == 'buy' and order in self.buy_orders:
            self.buy_orders.remove(order)
        elif order.order_type == 'sell' and order in self.sell_orders:
            self.sell_orders.remove(order)

    def __repr__(self):
        return f"OrderBook(Buys: {self.buy_orders}, Sells: {self.sell_orders})"


class OrderEngine:
    def __init__(self):
        self.order_book = OrderBook()
        self.trade_history = []

    def place_order(self, order):
        logging.info(f"Placing order: {order}")
        self.order_book.add_order(order)
        self.match_orders()

    def match_orders(self):
        """
        Continuously match the best (highest-price) buy order with the best (lowest-price) sell order.
        Orders must be for the same asset and the buy price must be at least the sell price.
        """
        while self.order_book.buy_orders and self.order_book.sell_orders:
            best_buy = self.order_book.buy_orders[0]
            best_sell = self.order_book.sell_orders[0]

            # Only match if assets are identical and pricing conditions are met.
            if best_buy.asset != best_sell.asset:
                logging.warning("Asset mismatch; cannot match orders.")
                break

            if best_buy.price >= best_sell.price:
                trade_price = best_sell.price  # Using seller's price
                trade_amount = min(best_buy.amount, best_sell.amount)
                trade = Trade(best_buy, best_sell, trade_amount, trade_price)
                logging.info(f"Match found: {trade}")
                self.execute_trade(trade, best_buy, best_sell)

                # Update orders to reflect the executed trade.
                best_buy.amount -= trade_amount
                best_sell.amount -= trade_amount

                # Update order statuses.
                if best_buy.amount <= 0:
                    best_buy.status = 'filled'
                    self.order_book.remove_order(best_buy)
                else:
                    best_buy.status = 'partial'

                if best_sell.amount <= 0:
                    best_sell.status = 'filled'
                    self.order_book.remove_order(best_sell)
                else:
                    best_sell.status = 'partial'
            else:
                # No match is possible when best buy price is lower than best sell price.
                break

    def execute_trade(self, trade, buy_order, sell_order):
        """
        Simulate trade execution including the creation of HTLC contracts.
        In a real system, this would trigger on-chain transactions with hash/time locks.
        """
        # Create a simulated HTLC contract.
        htlc = HTLCContract(buyer=buy_order.user_id, seller=sell_order.user_id, trade=trade)
        trade.htlc = htlc
        logging.info(f"Executing trade: {trade}")
        logging.info(f"Created HTLC: {htlc}")
        self.trade_history.append(trade)

    def simulate_secret_reveal(self, trade_id, secret):
        """
        Simulate the process of one party revealing the secret to settle an HTLC.
        """
        trade = next((t for t in self.trade_history if t.trade_id == trade_id), None)
        if trade and trade.htlc:
            success = trade.htlc.settle(secret)
            if success:
                logging.info(f"Trade {trade.trade_id[:8]} successfully settled.")
            else:
                logging.error(f"Trade {trade.trade_id[:8]} failed to settle.")
        else:
            logging.error("Trade not found or HTLC not created.")

    def __repr__(self):
        return f"OrderEngine(OrderBook: {self.order_book}, TradeHistory: {self.trade_history})"


# --- Example Usage ---

if __name__ == "__main__":
    engine = OrderEngine()

    # Create some sample orders.
    order1 = Order(order_type="buy", asset="BTC", amount=1.5, price=10000, user_id="UserA")
    order2 = Order(order_type="sell", asset="BTC", amount=1.0, price=9900, user_id="UserB")
    order3 = Order(order_type="sell", asset="BTC", amount=0.8, price=10000, user_id="UserC")
    order4 = Order(order_type="buy", asset="BTC", amount=0.5, price=9950, user_id="UserD")

    # Place orders into the engine.
    engine.place_order(order1)
    engine.place_order(order2)
    engine.place_order(order3)
    engine.place_order(order4)

    # For demonstration, simulate the secret reveal for the first trade in our trade history.
    if engine.trade_history:
        first_trade = engine.trade_history[0]
        # In a real scenario, the secret would be revealed by one party via a blockchain transaction.
        # Here we simulate it by directly using the secret from the HTLC.
        simulated_secret = first_trade.htlc.secret
        engine.simulate_secret_reveal(first_trade.trade_id, simulated_secret)

    # Display the final state of the order book and trade history.
    logging.info("Final Order Book:")
    logging.info(engine.order_book)
    logging.info("Trade History:")
    logging.info(engine.trade_history)

    # --- External Pricing Information ---
    # Retrieve and log external exchange rates for a few assets.
    for asset in ["BTC", "GRC"]:
        price = get_external_price(asset, vs_currency="usd")
        if price is not None:
            logging.info(f"External price for {asset} in USD: {price}")
