import dotenv from 'dotenv';
dotenv.config();

import fs from 'fs';
import path from 'path';
import { pathToFileURL } from 'url';
import { Buffer } from 'buffer';
import { Keypair, PublicKey, SystemProgram, Transaction, Connection, sendAndConfirmTransaction } from '@solana/web3.js';
import { TOKEN_PROGRAM_ID, getMinimumBalanceForRentExemptAccount, createInitializeAccountInstruction } from '@solana/spl-token';
import { DexInstructions } from '@project-serum/serum';
import BN from 'bn.js';

const MARKET_STATE_SPACE = 388;
const REQUEST_QUEUE_SPACE = 5120 + 12;
const EVENT_QUEUE_SPACE = 262144 + 12;
const BIDS_SPACE = 65536 + 12;
const ASKS_SPACE = 65536 + 12;
const TOKEN_ACCOUNT_SPACE = 165;

function assertPublicKey(name, value) {
  try {
    return new PublicKey(value);
  } catch {
    throw new Error(`${name} is not a valid public key: ${value}`);
  }
}

function loadKeypairFromFile(filePath) {
  const secret = JSON.parse(fs.readFileSync(filePath, 'utf8'));
  return Keypair.fromSecretKey(new Uint8Array(secret));
}

function envFirst(...values) {
  for (const value of values) {
    if (value !== undefined && value !== null && String(value).trim() !== '') {
      return value;
    }
  }
  return undefined;
}

export async function createSerumAccounts({ connection, payer, programId }) {
  const marketKeypair = Keypair.generate();
  const requestQueue = Keypair.generate();
  const eventQueue = Keypair.generate();
  const bids = Keypair.generate();
  const asks = Keypair.generate();

  const [marketRent, requestQueueRent, eventQueueRent, bidsRent, asksRent] = await Promise.all([
    connection.getMinimumBalanceForRentExemption(MARKET_STATE_SPACE),
    connection.getMinimumBalanceForRentExemption(REQUEST_QUEUE_SPACE),
    connection.getMinimumBalanceForRentExemption(EVENT_QUEUE_SPACE),
    connection.getMinimumBalanceForRentExemption(BIDS_SPACE),
    connection.getMinimumBalanceForRentExemption(ASKS_SPACE),
  ]);

  const tx = new Transaction().add(
    SystemProgram.createAccount({
      fromPubkey: payer.publicKey,
      newAccountPubkey: marketKeypair.publicKey,
      lamports: marketRent,
      space: MARKET_STATE_SPACE,
      programId,
    }),
    SystemProgram.createAccount({
      fromPubkey: payer.publicKey,
      newAccountPubkey: requestQueue.publicKey,
      lamports: requestQueueRent,
      space: REQUEST_QUEUE_SPACE,
      programId,
    }),
    SystemProgram.createAccount({
      fromPubkey: payer.publicKey,
      newAccountPubkey: eventQueue.publicKey,
      lamports: eventQueueRent,
      space: EVENT_QUEUE_SPACE,
      programId,
    }),
    SystemProgram.createAccount({
      fromPubkey: payer.publicKey,
      newAccountPubkey: bids.publicKey,
      lamports: bidsRent,
      space: BIDS_SPACE,
      programId,
    }),
    SystemProgram.createAccount({
      fromPubkey: payer.publicKey,
      newAccountPubkey: asks.publicKey,
      lamports: asksRent,
      space: ASKS_SPACE,
      programId,
    }),
  );

  const signature = await sendAndConfirmTransaction(connection, tx, [payer, marketKeypair, requestQueue, eventQueue, bids, asks]);

  return {
    signature,
    marketKeypair,
    requestQueue,
    eventQueue,
    bids,
    asks,
  };
}

export async function initializeSerumMarket({
  connection,
  payer,
  programId,
  baseMint,
  quoteMint,
  baseLotSize,
  quoteLotSize,
  marketKeypair,
  requestQueue,
  eventQueue,
  bids,
  asks,
  feeRateBps = 0,
  quoteDustThreshold = 100,
}) {
  const normalizedBaseLotSize = BN.isBN(baseLotSize) ? baseLotSize : new BN(baseLotSize);
  const normalizedQuoteLotSize = BN.isBN(quoteLotSize) ? quoteLotSize : new BN(quoteLotSize);
  const normalizedQuoteDustThreshold = BN.isBN(quoteDustThreshold) ? quoteDustThreshold : new BN(quoteDustThreshold);

  let vaultOwner;
  let vaultSignerNonce = new BN(0);

  for (;;) {
    try {
      vaultOwner = await PublicKey.createProgramAddress(
        [
          marketKeypair.publicKey.toBuffer(),
          vaultSignerNonce.toArrayLike(Buffer, 'le', 8),
        ],
        programId,
      );
      break;
    } catch {
      vaultSignerNonce = vaultSignerNonce.addn(1);
    }
  }
  const vaultLamports = await getMinimumBalanceForRentExemptAccount(connection);

  const baseVault = Keypair.generate();
  const quoteVault = Keypair.generate();

  const tx = new Transaction().add(
    SystemProgram.createAccount({
      fromPubkey: payer.publicKey,
      newAccountPubkey: baseVault.publicKey,
      lamports: vaultLamports,
      space: TOKEN_ACCOUNT_SPACE,
      programId: TOKEN_PROGRAM_ID,
    }),
    createInitializeAccountInstruction(baseVault.publicKey, baseMint, vaultOwner, TOKEN_PROGRAM_ID),
    SystemProgram.createAccount({
      fromPubkey: payer.publicKey,
      newAccountPubkey: quoteVault.publicKey,
      lamports: vaultLamports,
      space: TOKEN_ACCOUNT_SPACE,
      programId: TOKEN_PROGRAM_ID,
    }),
    createInitializeAccountInstruction(quoteVault.publicKey, quoteMint, vaultOwner, TOKEN_PROGRAM_ID),
    DexInstructions.initializeMarket({
      market: marketKeypair.publicKey,
      requestQueue: requestQueue.publicKey,
      eventQueue: eventQueue.publicKey,
      bids: bids.publicKey,
      asks: asks.publicKey,
      baseVault: baseVault.publicKey,
      quoteVault: quoteVault.publicKey,
      baseMint,
      quoteMint,
      baseLotSize: normalizedBaseLotSize,
      quoteLotSize: normalizedQuoteLotSize,
      feeRateBps,
      vaultSignerNonce,
      quoteDustThreshold: normalizedQuoteDustThreshold,
      programId,
      authority: undefined,
    }),
  );

  const signature = await sendAndConfirmTransaction(connection, tx, [payer, baseVault, quoteVault]);

  return {
    signature,
    marketAddress: marketKeypair.publicKey,
    baseVault: baseVault.publicKey,
    quoteVault: quoteVault.publicKey,
    vaultOwner,
    vaultSignerNonce,
  };
}

export async function createSerumMarket({
  rpcUrl,
  payerPath,
  programId,
  baseMint,
  quoteMint,
  baseLotSize,
  quoteLotSize,
  feeRateBps = 0,
  quoteDustThreshold = 100,
  commitment = 'confirmed',
}) {
  const connection = new Connection(rpcUrl, commitment);
  const payer = loadKeypairFromFile(payerPath);
  const serumProgramId = assertPublicKey('SERUM_DEX_PROGRAM_ID', programId);
  const baseMintKey = assertPublicKey('BASE_MINT', baseMint);
  const quoteMintKey = assertPublicKey('QUOTE_MINT', quoteMint);

  const payerBalance = await connection.getBalance(payer.publicKey);
  console.log('RPC URL:', rpcUrl);
  console.log('Payer:', payer.publicKey.toBase58());
  console.log('Payer path:', payerPath);
  console.log('Payer balance:', payerBalance);
  console.log('Program ID:', serumProgramId.toBase58());
  console.log('Base mint:', baseMintKey.toBase58());
  console.log('Quote mint:', quoteMintKey.toBase58());
  console.log('Base lot size:', String(baseLotSize));
  console.log('Quote lot size:', String(quoteLotSize));
  console.log('Fee rate bps:', feeRateBps);
  console.log('Quote dust threshold:', String(quoteDustThreshold));

  const accounts = await createSerumAccounts({ connection, payer, programId: serumProgramId });
  const init = await initializeSerumMarket({
    connection,
    payer,
    programId: serumProgramId,
    baseMint: baseMintKey,
    quoteMint: quoteMintKey,
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

  return {
    createAccountsSignature: accounts.signature,
    initializeMarketSignature: init.signature,
    marketAddress: init.marketAddress,
    baseVault: init.baseVault,
    quoteVault: init.quoteVault,
    vaultOwner: init.vaultOwner,
    vaultSignerNonce: init.vaultSignerNonce,
  };
}

async function main() {
  const result = await createSerumMarket({
    rpcUrl: envFirst(process.env.DEVNET_RPC_URL, process.env.VITE_DEVNET_RPC_URL),
    payerPath: envFirst(
      process.env.SOLANA_PAYER_KEYPAIR_PATH,
      process.env.SOLANA_KEYPAIR_PATH,
      path.resolve(process.cwd(), 'wgrc.json'),
    ),
    programId: envFirst(process.env.SERUM_DEX_PROGRAM_ID, process.env.VITE_SERUM_DEX_PROGRAM_ID),
    baseMint: envFirst(process.env.WGR_C_TOKEN_ADDRESS, process.env.VITE_WGR_C_TOKEN_ADDRESS, process.env.VITE_BASE_MINT),
    quoteMint: envFirst(process.env.SOL_TOKEN_ADDRESS, process.env.VITE_SOL_TOKEN_ADDRESS, process.env.VITE_QUOTE_MINT),
    baseLotSize: new BN(envFirst(process.env.BASE_LOT_SIZE, process.env.VITE_BASE_LOT_SIZE, '1000000')),
    quoteLotSize: new BN(envFirst(process.env.QUOTE_LOT_SIZE, process.env.VITE_QUOTE_LOT_SIZE, '100000000')),
    feeRateBps: Number(envFirst(process.env.FEE_RATE_BPS, process.env.VITE_FEE_RATE_BPS, '0')),
    quoteDustThreshold: new BN(envFirst(process.env.QUOTE_DUST_THRESHOLD, process.env.VITE_QUOTE_DUST_THRESHOLD, '100')),
  });

  console.log('Create-accounts tx:', result.createAccountsSignature);
  console.log('Initialize-market tx:', result.initializeMarketSignature);
  console.log('New market:', result.marketAddress.toBase58());
  console.log('Base vault:', result.baseVault.toBase58());
  console.log('Quote vault:', result.quoteVault.toBase58());
}

const isDirectRun = process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url;
if (isDirectRun) {
  main().catch((error) => {
    console.error('Error creating Serum market:', error);
    process.exitCode = 1;
  });
}
