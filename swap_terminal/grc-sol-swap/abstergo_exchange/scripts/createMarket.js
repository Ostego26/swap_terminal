/**
 * createMarket.js
 * 
 * A fully implemented example script to create a Serum market for the wGRC/SOL pair on Solana Devnet.
 * 
 * This script loads configuration from environment variables (via dotenv), checks if the payer has enough lamports
 * (and requests an airdrop if not), creates all required accounts (split into two transactions to avoid transaction size limits),
 * and then initializes the Serum market.
 *
 * Usage: node scripts/createMarket.js
 *
 * IMPORTANT: You must adjust account sizes, lot sizes, fee rates, and other parameters based on the latest Serum DEX specifications.
 * Refer to the Serum DEX documentation for complete details.
 */

require('dotenv').config();
const fs = require('fs');
const { Connection, PublicKey, Keypair, SystemProgram, Transaction } = require('@solana/web3.js');
const BN = require('bn.js');
const { DexInstructions } = require('@project-serum/serum');

// Load configuration from environment variables
const DEVNET_RPC_URL = process.env.DEVNET_RPC_URL;
const PAYER_KEYPAIR_PATH = process.env.PAYER_KEYPAIR_PATH;
const BASE_MINT = new PublicKey(process.env.BASE_MINT);       // wGRC token mint
const QUOTE_MINT = new PublicKey(process.env.QUOTE_MINT);       // SOL token mint
const SERUM_DEX_PROGRAM_ID = new PublicKey(process.env.SERUM_DEX_PROGRAM_ID);
const baseLotSize = new BN(process.env.BASE_LOT_SIZE || '1000');
const quoteLotSize = new BN(process.env.QUOTE_LOT_SIZE || '100');
const feeRateBps = Number(process.env.FEE_RATE_BPS || '0');
const quoteDustThreshold = new BN(process.env.QUOTE_DUST_THRESHOLD || '100');

// Set up connection to Devnet
const connection = new Connection(DEVNET_RPC_URL, 'confirmed');

// Load the payer keypair from file
const payer = Keypair.fromSecretKey(
  Uint8Array.from(JSON.parse(fs.readFileSync(PAYER_KEYPAIR_PATH)))
);

console.log('--- Serum Market Creation Script ---');
console.log('Using Devnet RPC URL:', DEVNET_RPC_URL);
console.log('Payer public key:', payer.publicKey.toBase58());
console.log('wGRC (base mint):', BASE_MINT.toBase58());
console.log('SOL (quote mint):', QUOTE_MINT.toBase58());
console.log('Serum DEX Program ID:', SERUM_DEX_PROGRAM_ID.toBase58());

// Define required account sizes (example values based on Serum DEX specifications)
const MARKET_ACCOUNT_SPACE = 3888;
const BIDS_ACCOUNT_SPACE = 65536;
const ASKS_ACCOUNT_SPACE = 65536;
const EVENT_QUEUE_ACCOUNT_SPACE = 262144;
const REQUEST_QUEUE_ACCOUNT_SPACE = 32768;
const VAULT_ACCOUNT_SPACE = 165;

/**
 * Utility function to get the minimum balance for rent exemption.
 * @param {number} space - The account space in bytes.
 * @returns {Promise<number>} - The lamports required.
 */
async function getRent(space) {
  return await connection.getMinimumBalanceForRentExemption(space);
}

(async () => {
  try {
    console.log('Calculating rent exemptions...');
    const marketRent = await getRent(MARKET_ACCOUNT_SPACE);
    const bidsRent = await getRent(BIDS_ACCOUNT_SPACE);
    const asksRent = await getRent(ASKS_ACCOUNT_SPACE);
    const eventQueueRent = await getRent(EVENT_QUEUE_ACCOUNT_SPACE);
    const requestQueueRent = await getRent(REQUEST_QUEUE_ACCOUNT_SPACE);
    const vaultRent = await getRent(VAULT_ACCOUNT_SPACE);
    
    console.log('Rent exemptions (lamports):');
    console.log('Market:', marketRent);
    console.log('Bids:', bidsRent);
    console.log('Asks:', asksRent);
    console.log('Event Queue:', eventQueueRent);
    console.log('Request Queue:', requestQueueRent);
    console.log('Vault:', vaultRent);
    
    // Before sending transactions, ensure the payer has enough balance.
    const currentBalance = await connection.getBalance(payer.publicKey);
    // For transaction 1, we need at least the sum for market, bids, asks, and event queue.
    const requiredTx1 = marketRent + bidsRent + asksRent + eventQueueRent;
    console.log('Current payer balance:', currentBalance, 'lamports');
    if (currentBalance < requiredTx1) {
      const deficit = requiredTx1 - currentBalance;
      console.log(`Insufficient balance. Requesting airdrop of ${deficit} lamports (~${(deficit/1e9).toFixed(2)} SOL)...`);
      const airdropSig = await connection.requestAirdrop(payer.publicKey, deficit + 1e9); // add extra SOL for fees
      await connection.confirmTransaction(airdropSig);
      const newBalance = await connection.getBalance(payer.publicKey);
      console.log('New payer balance:', newBalance, 'lamports');
    }
    
    // Generate new keypairs for required accounts
    const marketAccount = Keypair.generate();
    const bidsAccount = Keypair.generate();
    const asksAccount = Keypair.generate();
    const eventQueueAccount = Keypair.generate();
    const requestQueueAccount = Keypair.generate();
    const baseVault = Keypair.generate();
    const quoteVault = Keypair.generate();
    
    // Split account creation into two transactions to avoid exceeding the transaction size limit.
    // Transaction 1: Create Market, Bids, Asks, and Event Queue accounts.
    const tx1 = new Transaction();
    tx1.add(
      SystemProgram.createAccount({
        fromPubkey: payer.publicKey,
        newAccountPubkey: marketAccount.publicKey,
        lamports: marketRent,
        space: MARKET_ACCOUNT_SPACE,
        programId: SERUM_DEX_PROGRAM_ID,
      }),
      SystemProgram.createAccount({
        fromPubkey: payer.publicKey,
        newAccountPubkey: bidsAccount.publicKey,
        lamports: bidsRent,
        space: BIDS_ACCOUNT_SPACE,
        programId: SERUM_DEX_PROGRAM_ID,
      }),
      SystemProgram.createAccount({
        fromPubkey: payer.publicKey,
        newAccountPubkey: asksAccount.publicKey,
        lamports: asksRent,
        space: ASKS_ACCOUNT_SPACE,
        programId: SERUM_DEX_PROGRAM_ID,
      }),
      SystemProgram.createAccount({
        fromPubkey: payer.publicKey,
        newAccountPubkey: eventQueueAccount.publicKey,
        lamports: eventQueueRent,
        space: EVENT_QUEUE_ACCOUNT_SPACE,
        programId: SERUM_DEX_PROGRAM_ID,
      })
    );
    
    console.log('Sending transaction 1 to create Market, Bids, Asks, and Event Queue accounts...');
    const txId1 = await connection.sendTransaction(tx1, [
      payer,
      marketAccount,
      bidsAccount,
      asksAccount,
      eventQueueAccount,
    ]);
    console.log('Transaction 1 sent, txId:', txId1);
    await connection.confirmTransaction(txId1);
    console.log('Transaction 1 confirmed.');
    
    // Transaction 2: Create Request Queue, Base Vault, and Quote Vault accounts.
    const tx2 = new Transaction();
    tx2.add(
      SystemProgram.createAccount({
        fromPubkey: payer.publicKey,
        newAccountPubkey: requestQueueAccount.publicKey,
        lamports: requestQueueRent,
        space: REQUEST_QUEUE_ACCOUNT_SPACE,
        programId: SERUM_DEX_PROGRAM_ID,
      }),
      SystemProgram.createAccount({
        fromPubkey: payer.publicKey,
        newAccountPubkey: baseVault.publicKey,
        lamports: vaultRent,
        space: VAULT_ACCOUNT_SPACE,
        programId: SERUM_DEX_PROGRAM_ID,
      }),
      SystemProgram.createAccount({
        fromPubkey: payer.publicKey,
        newAccountPubkey: quoteVault.publicKey,
        lamports: vaultRent,
        space: VAULT_ACCOUNT_SPACE,
        programId: SERUM_DEX_PROGRAM_ID,
      })
    );
    
    console.log('Sending transaction 2 to create Request Queue, Base Vault, and Quote Vault accounts...');
    const txId2 = await connection.sendTransaction(tx2, [
      payer,
      requestQueueAccount,
      baseVault,
      quoteVault,
    ]);
    console.log('Transaction 2 sent, txId:', txId2);
    await connection.confirmTransaction(txId2);
    console.log('Transaction 2 confirmed.');
    
    // Initialize the Serum market using DexInstructions.initializeMarket.
    // IMPORTANT: The initializeMarket API may vary between Serum SDK versions.
    console.log('Initializing Serum market...');
    const initTxId = await DexInstructions.initializeMarket(
      connection,
      payer,
      {
        market: marketAccount.publicKey,
        bids: bidsAccount.publicKey,
        asks: asksAccount.publicKey,
        eventQueue: eventQueueAccount.publicKey,
        requestQueue: requestQueueAccount.publicKey,
        baseVault: baseVault.publicKey,
        quoteVault: quoteVault.publicKey,
        baseMint: BASE_MINT,
        quoteMint: QUOTE_MINT,
        baseLotSize,
        quoteLotSize,
        feeRateBps,
        quoteDustThreshold,
      },
      SERUM_DEX_PROGRAM_ID
    );
    console.log('Serum market initialized successfully. Transaction ID:', initTxId);
    console.log('New Serum Market Address:', marketAccount.publicKey.toBase58());
  } catch (error) {
    console.error('Error creating Serum market:', error);
  }
})();
