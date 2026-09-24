import { Connection, PublicKey, Transaction, SystemProgram, Keypair } from '@solana/web3.js';
import { Market } from '@project-serum/serum';
import axios from 'axios';

const connection = new Connection(process.env.VITE_DEVNET_RPC_URL, 'confirmed');
const SERUM_MARKET_ADDRESS = new PublicKey(process.env.SERUM_MARKET_ADDRESS);
const SOL_TOKEN_ADDRESS = new PublicKey(process.env.SOL_TOKEN_ADDRESS);

// Convert GRC to SOL with dynamic conversion rate from CoinGecko
export async function convertGrcToSol(grcAmount) {
  const prices = await axios.get(process.env.VITE_API_URL);
  const grcPriceInUSD = prices.data['wgrc']?.usd || 0;
  const solPriceInUSD = prices.data['solana']?.usd || 0;

  const grcToUsd = grcAmount * grcPriceInUSD;
  const solAmount = grcToUsd / solPriceInUSD;

  console.log(`Converted ${grcAmount} GRC to ${solAmount} SOL`);
  return solAmount;
}

// Create a Serum swap transaction
export async function createSwapTransaction(userAddress, solAmount) {
  const market = await Market.load(connection, SERUM_MARKET_ADDRESS, {}, process.env.SERUM_DEX_PROGRAM_ID);
  const orderParams = {
    side: 'buy',
    price: 1, // This should be dynamic, depending on the market conditions
    size: solAmount,
    orderType: 'limit',
    owner: userAddress,
    payer: SOL_TOKEN_ADDRESS,
  };

  const order = await market.makeOrder(connection, orderParams);
  const txId = await market.placeOrder(connection, order);

  console.log('Solana transaction placed:', txId);
  return txId;
}
