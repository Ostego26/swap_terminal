import os from 'os';
import path from 'path';
import { Keypair, Connection, PublicKey } from '@solana/web3.js';
import { readFileSync } from 'fs';
import dotenv from 'dotenv';

dotenv.config();

function envFirst(...values) {
  for (const value of values) {
    if (value !== undefined && value !== null && String(value).trim() !== '') {
      return value;
    }
  }
  return undefined;
}

const devnetRpcUrl = envFirst(process.env.DEVNET_RPC_URL, process.env.VITE_DEVNET_RPC_URL);
const serumDexProgramId = envFirst(process.env.SERUM_DEX_PROGRAM_ID, process.env.VITE_SERUM_DEX_PROGRAM_ID);
const baseMint = envFirst(process.env.WGR_C_TOKEN_ADDRESS, process.env.VITE_WGR_C_TOKEN_ADDRESS, process.env.VITE_BASE_MINT);
const quoteMint = envFirst(process.env.SOL_TOKEN_ADDRESS, process.env.VITE_SOL_TOKEN_ADDRESS, process.env.VITE_QUOTE_MINT);
const keypairPath = envFirst(
  process.env.SOLANA_PAYER_KEYPAIR_PATH,
  process.env.SOLANA_KEYPAIR_PATH,
  path.resolve(process.cwd(), 'wgrc.json'),
  path.join(os.homedir(), '.config/solana/id.json'),
);

const secretKey = JSON.parse(readFileSync(keypairPath, 'utf8'));
const payer = Keypair.fromSecretKey(new Uint8Array(secretKey));

async function inspectMarket() {
  const connection = new Connection(devnetRpcUrl, 'confirmed');
  const programId = new PublicKey(serumDexProgramId);
  const baseMintPubkey = new PublicKey(baseMint);
  const quoteMintPubkey = new PublicKey(quoteMint);
  const payerBalance = await connection.getBalance(payer.publicKey);

  console.log('RPC URL:', devnetRpcUrl);
  console.log('Keypair path:', keypairPath);
  console.log('Payer:', payer.publicKey.toBase58());
  console.log('Payer balance:', payerBalance);
  console.log('Base Mint:', baseMintPubkey.toBase58());
  console.log('Quote Mint:', quoteMintPubkey.toBase58());
  console.log('Serum Dex Program ID:', programId.toBase58());
}

inspectMarket().catch((error) => {
  console.error('Error inspecting market config:', error);
  process.exitCode = 1;
});
