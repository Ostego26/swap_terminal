import { PublicKey, Keypair } from '@solana/web3.js'; // Keypair import

/**
 * Helper function to create PublicKey from environment variable.
 * @param {string} variableName - Name of the environment variable.
 * @returns {PublicKey} - The Solana PublicKey instance.
 */
function createPublicKeyFromEnv(variableName) {
  const address = process.env[variableName];
  
  if (!address) {
    console.error(`${variableName} is not defined in the environment.`);
    throw new Error(`${variableName} is missing.`);
  }

  try {
    console.log(`Creating PublicKey from address: ${address}`);
    return new PublicKey(address);  // This will throw if the address is invalid
  } catch (error) {
    console.error(`Invalid PublicKey format for ${variableName}: ${address}`);
    throw new Error(`Invalid PublicKey format for ${variableName}`);
  }
}

// SOL doesn't require a mint address for transactions, so it will be set to `null`
const SOL_TOKEN_ADDRESS = null; // SOL is handled natively, so no mint address is needed

// Ensure that the token address provided is a valid mint address (check this address on Solana explorer)
let WGR_C_TOKEN_ADDRESS = null;
if (process.env.REACT_APP_WGR_C_TOKEN_ADDRESS) {
  try {
    WGR_C_TOKEN_ADDRESS = createPublicKeyFromEnv('REACT_APP_WGR_C_TOKEN_ADDRESS');
  } catch (error) {
    console.error('WGR_C_TOKEN_ADDRESS is missing or invalid');
    throw new Error('Please provide a valid wGRC token mint address.');
  }
} else {
  console.error('REACT_APP_WGR_C_TOKEN_ADDRESS is not defined.');
}

/**
 * Validates if the side is either 'buy' or 'sell'.
 * @param {string} side - Order side ('buy' or 'sell').
 */
function validateOrderSide(side) {
  if (!['buy', 'sell'].includes(side)) {
    console.error('Invalid order side:', side);
    throw new Error('Order side must be either "buy" or "sell".');
  }
}

/**
 * Validates if the price and size are valid numbers.
 * @param {number|string} price - The order price.
 * @param {number|string} size - The order size.
 */
function validatePriceAndSize(price, size) {
  if (isNaN(price) || isNaN(size)) {
    console.error('Invalid price or size:', price, size);
    throw new Error('Price and size must be valid numbers.');
  }
}

/**
 * Place an order on the Serum market.
 * @param {string} side - 'buy' or 'sell'
 * @param {number|string} price - Order price.
 * @param {number|string} size - Order size.
 * @param {object} userWalletKeypair - The wallet keypair for signing.
 * @param {object} market - The Serum market instance.
 * @returns {Promise<string>} - The transaction ID.
 */
export async function placeOrder(side, price, size, userWalletKeypair, market) {
  try {
    // Validate side, price, and size
    validateOrderSide(side);
    validatePriceAndSize(price, size);

    const payer = side === 'buy' ? SOL_TOKEN_ADDRESS : WGR_C_TOKEN_ADDRESS;

    if (!payer) {
      throw new Error('Payer address is missing.');
    }

    const orderParams = {
      owner: userWalletKeypair,
      payer,
      side,
      price: parseFloat(price),
      size: parseFloat(size),
      orderType: 'limit',
    };

    console.log('Creating order with params:', orderParams);

    // Place the order
    const order = await market.makeOrder(market.connection, orderParams);
    const txId = await market.placeOrder(market.connection, order);
    console.log('Order placed successfully, txId:', txId);
    return txId;
  } catch (error) {
    console.error('Error placing order:', error);
    throw new Error(error.message);
  }
}

/**
 * Fetches the order book for a given market.
 * @param {object} market - The Serum market instance.
 * @returns {Promise<Object>} - The order book.
 */
export async function fetchOrderBook(market) {
  try {
    console.log('Fetching order book for the market...');
    const bids = await market.loadBids();
    const asks = await market.loadAsks();
    console.log('Order book fetched: Bids and Asks');

    // Return the top 5 bid and ask levels
    return { bids: bids.getL2(5), asks: asks.getL2(5) };
  } catch (error) {
    console.error('Error fetching order book:', error);
    throw new Error('Failed to fetch order book');
  }
}

/**
 * Fetches the current market price from the order book.
 * @param {object} market - The Serum market instance.
 * @returns {Promise<Object>} - The current market price.
 */
export async function getMarketPrice(market) {
  try {
    const orderBook = await fetchOrderBook(market);
    const bestBid = orderBook.bids[0];
    const bestAsk = orderBook.asks[0];

    const marketPrice = {
      bid: bestBid ? bestBid[0] : null, // Best bid price
      ask: bestAsk ? bestAsk[0] : null,  // Best ask price
    };

    console.log('Current market price:', marketPrice);
    return marketPrice;
  } catch (error) {
    console.error('Error getting market price:', error);
    throw new Error('Failed to get market price');
  }
}

/**
 * Helper function to create and return a Solana wallet keypair.
 * @returns {Keypair} - A new Solana wallet keypair.
 */
export function createWallet() {
  try {
    const keypair = Keypair.generate();
    console.log('Wallet created successfully:', keypair.publicKey.toBase58());
    return keypair;
  } catch (error) {
    console.error('Error creating wallet:', error);
    throw new Error('Failed to create wallet');
  }
}
