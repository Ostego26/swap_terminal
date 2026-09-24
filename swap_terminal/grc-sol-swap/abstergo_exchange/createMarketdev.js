import dotenv from 'dotenv';
dotenv.config();

import path from 'path';
import { Connection, Keypair, PublicKey } from '@solana/web3.js';
import { readFileSync } from 'fs';
import BN from 'bn.js';
import { createSerumAccounts, initializeSerumMarket } from './createMarket.js';

function envFirst(...values) {
  for (const value of values) {
    if (value !== undefined && value !== null && String(value).trim() !== '') {
      return value;
    }
  }
  return undefined;
}

const connection = new Connection(envFirst(process.env.DEVNET_RPC_URL, process.env.VITE_DEVNET_RPC_URL, 'https://api.devnet.solana.com'), 'confirmed');
const serumProgramId = new PublicKey(envFirst(process.env.SERUM_DEX_PROGRAM_ID, process.env.VITE_SERUM_DEX_PROGRAM_ID, 'DESVgJVGajEgKGXhb6XmqDHGz3VjdgP7rEVESBgxmroY'));
const payerPath = envFirst(
  process.env.SOLANA_PAYER_KEYPAIR_PATH,
  process.env.SOLANA_KEYPAIR_PATH,
  path.resolve(process.cwd(), 'wgrc.json'),
);
const secret = JSON.parse(readFileSync(payerPath, 'utf8'));
const payer = Keypair.fromSecretKey(new Uint8Array(secret));
const baseMint = new PublicKey(envFirst(process.env.WGR_C_TOKEN_ADDRESS, process.env.VITE_WGR_C_TOKEN_ADDRESS, process.env.VITE_BASE_MINT));
const quoteMint = new PublicKey(envFirst(process.env.SOL_TOKEN_ADDRESS, process.env.VITE_SOL_TOKEN_ADDRESS, process.env.VITE_QUOTE_MINT));
const baseLotSize = new BN(envFirst(process.env.BASE_LOT_SIZE, process.env.VITE_BASE_LOT_SIZE, '1000000'));
const quoteLotSize = new BN(envFirst(process.env.QUOTE_LOT_SIZE, process.env.VITE_QUOTE_LOT_SIZE, '100000000'));
const feeRateBps = Number(envFirst(process.env.FEE_RATE_BPS, process.env.VITE_FEE_RATE_BPS, '0'));
const quoteDustThreshold = new BN(envFirst(process.env.QUOTE_DUST_THRESHOLD, process.env.VITE_QUOTE_DUST_THRESHOLD, '100'));

async function main() {
  console.log('Using Serum program:', serumProgramId.toBase58());
  console.log('Using payer path:', payerPath);
  console.log('Using payer:', payer.publicKey.toBase58());
  console.log('Using base mint:', baseMint.toBase58());
  console.log('Using quote mint:', quoteMint.toBase58());

  const accounts = await createSerumAccounts({
    connection,
    payer,
    programId: serumProgramId,
  });

  console.log('Created large market accounts:', accounts.marketKeypair.publicKey.toBase58());
  console.log('Create-accounts tx:', accounts.signature);

  const market = await initializeSerumMarket({
    connection,
    payer,
    programId: serumProgramId,
    baseMint,
    quoteMint,
    baseLotSize,
    quoteLotSize,
    marketKeypair: accounts.marketKeypair,
    requestQueue: accounts.requestQueue,
    eventQueue: accounts.eventQueue,
    bids: accounts.bids,
    asks: accounts.asks,
    feeRateBps,
    quoteDustThreshold,
  });

  console.log('Initialize-market tx:', market.signature);
  console.log('New devnet Serum market address =>', market.marketAddress.toBase58());
}

main().catch((err) => {
  console.error('Error creating Serum market:', err);
  process.exitCode = 1;
});
