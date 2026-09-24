/**
 * A second Express server: accept a GRC deposit request and wrap it to wGRC.
 *
 * Role: file (entry point -- would be `node services/gridcoin.js`)
 * Reads: the process environment (GRIDCOIN_RPC_URL, EXCHANGE_WALLET_ADDRESS,
 *        VITE_DEVNET_RPC_URL, PORT)
 * Writes: nothing locally -- AND THE GRIDCOIN CHAIN, via the RPC below
 * Can move funds: YES. `handleGridcoinSwap` calls the Gridcoin RPC method
 *        `sendtoaddress` with an amount taken from the request body. That is a
 *        wallet spend and it is final once relayed.
 * Mainnet-safe: NO. There is no network selector here at all; whatever
 *        GRIDCOIN_RPC_URL points at is what gets spent from.
 *
 * ---------------------------------------------------------------------------
 * READ THIS BEFORE RUNNING IT. Measured 2026-09-24.
 * ---------------------------------------------------------------------------
 *
 * WHAT IT WAS. `POST /deposit` took `{ amount }` from an unauthenticated
 * request body and called `sendtoaddress` with it. No caller check, no ceiling
 * on the amount, no idempotency, no record written anywhere. Anyone who could
 * reach the port could drain the Gridcoin wallet one request at a time, and
 * nothing in the tree would have a row saying it happened.
 *
 * It is also almost certainly not what the name suggests. A route called
 * "deposit" that sends FROM the operator's own wallet TO the operator's own
 * EXCHANGE_WALLET_ADDRESS is not a customer depositing; it is an internal
 * sweep with a customer-facing name. `wrapGrcToWgrc` is an empty function with
 * a comment saying the minting logic "goes here", so the second half of what
 * this route claims to do does not exist. src/GridcoinBalance.jsx:38 posts to
 * it from the browser.
 *
 * WHY IT IS STILL HERE. Rule 2 says prove a thing is dead before deleting it,
 * and rule 2's own guard says "I could not find a caller" is not "there is no
 * caller". What was established, by grepping the whole tree for the filename
 * and for the route: nothing in package.json starts it, no shell script or
 * launcher names `services/gridcoin.js`, and it binds `process.env.PORT || 5000`
 * -- the SAME port server.js defaults to, so the two cannot both be running.
 * What was NOT established: whether the operator starts it by hand. Deleting a
 * fund-moving entry point on that evidence is not a call this pass gets to
 * make (rule 16), so it is gated, documented, and handed back.
 *
 * WHAT CHANGED. The shared secret is now required on `/deposit`, using the same
 * constant-time check as server.js (auth.js). That strictly REMOVES the ability
 * to move funds from unauthenticated callers and adds none. It does break
 * src/GridcoinBalance.jsx, which has no secret to present -- deliberately, and
 * said here rather than discovered later: a browser button that spends from the
 * hot wallet with no authentication is the defect, not the thing to preserve.
 * Nothing else in this file's behavior was touched.
 */

import express from 'express';
// Connection is the only Solana import this file uses; PublicKey, Keypair,
// Transaction and SystemProgram were imported and never referenced (rule 9,
// and F401's JavaScript equivalent). They were the vocabulary of the wGRC
// minting that wrapGrcToWgrc never got, and importing them made the file look
// like it did more than it does.
import { Connection } from '@solana/web3.js';
import axios from 'axios';
import dotenv from 'dotenv';
import cors from 'cors';

import { describeSharedSecretConfig, requireSharedSecret } from '../auth.js';

// Load environment variables
dotenv.config();

// Setup express app
const app = express();
const port = process.env.PORT || 5000;
const solanaConnection = new Connection(process.env.VITE_DEVNET_RPC_URL, 'confirmed');
app.use(cors());
app.use(express.json()); // Allow POST requests to be parsed as JSON

/**
 * The same shared-secret configuration server.js uses, resolved the same way.
 *
 * `payoutEnabled: true` is not a guess: this process can spend from the
 * Gridcoin wallet unconditionally, so the fatal combination in
 * describeSharedSecretConfig (enforcement disabled while armed) applies to it
 * with no qualification. There is no read-only mode here to fall back to.
 */
const SHARED_SECRET_CONFIG = {
  enforcementEnabled: String(process.env.REQUIRE_GRIDCOIN_SHARED_SECRET || 'true').toLowerCase() !== 'false',
  secret: process.env.GRIDCOIN_VERIFY_SHARED_SECRET || null,
};

const SHARED_SECRET_STATUS = describeSharedSecretConfig({
  ...SHARED_SECRET_CONFIG,
  payoutEnabled: true,
  armedDescription: 'POST /deposit -- a Gridcoin sendtoaddress with an amount taken from the request body and no ceiling',
});
for (const line of SHARED_SECRET_STATUS.lines) console.log(line);
if (SHARED_SECRET_STATUS.fatal) throw new Error(SHARED_SECRET_STATUS.fatal);

// API Endpoint to handle Gridcoin deposit.
//
// AUTHENTICATION: NOW REQUIRED. It had none, and it spends. See the file
// header for what this route actually does and why it was not deleted.
app.post('/deposit', async (req, res) => {
  try {
    requireSharedSecret(req, SHARED_SECRET_CONFIG);
  } catch (error) {
    return res.status(error.statusCode || 401).json({ error: error.message });
  }

  const { amount } = req.body;

  if (!amount || isNaN(amount) || parseFloat(amount) <= 0) {
    return res.status(400).json({ error: 'Invalid amount provided' });
  }

  try {
    console.log(`Depositing ${amount} GRC to exchange wallet...`);
    const depositResult = await handleGridcoinSwap(amount, process.env.EXCHANGE_WALLET_ADDRESS);

    if (depositResult) {
      console.log(`Deposit successful for ${amount} GRC.`);

      // Wrap the GRC into wGRC
      await wrapGrcToWgrc(amount, process.env.EXCHANGE_WALLET_ADDRESS);
      return res.json({ success: true, message: `Successfully deposited ${amount} GRC.` });
    } else {
      return res.status(400).json({ error: 'Deposit failed' });
    }
  } catch (error) {
    // Annotated, not changed (rule 12's BLE001 test). The caller is told 500
    // and the real cause is printed, so a failure cannot be mistaken here for
    // a success. What this DOES hide is whether the send happened: see the
    // note in handleGridcoinSwap.
    console.error('Error handling deposit:', error);
    return res.status(500).json({ error: 'Internal Server Error' });
  }
});

// Handle Gridcoin deposit transfer to exchange wallet
async function handleGridcoinSwap(grcAmount, exchangeWalletAddress) {
  try {
    const response = await axios.post(process.env.GRIDCOIN_RPC_URL, {
      jsonrpc: '2.0',
      method: 'sendtoaddress',
      params: [exchangeWalletAddress, grcAmount], // Send Gridcoin to the exchange wallet
      id: 1
    });

    if (response.data.error) {
      console.error('Gridcoin RPC error:', response.data.error);
      throw new Error('Error during Gridcoin transfer');
    }

    return response.data.result;
  } catch (error) {
    // Left as it was found, and annotated rather than changed (rule 12's
    // BLE001 test): the caller CAN tell the failure from a real answer,
    // because this rethrows rather than returning a value the route would read
    // as success. What it does lose is WHICH failure -- an RPC that refused
    // and a socket that timed out both surface as 'Gridcoin RPC failed' -- and
    // on a spend path that distinction matters: the first means nothing was
    // sent, the second means something may have been and there is no txid to
    // look for. Narrowing it changes the fund-moving path and is the
    // operator's (rule 16).
    console.error('Error during Gridcoin transfer:', error);
    throw new Error('Gridcoin RPC failed');
  }
}

// Wrap GRC into wGRC logic (implement this part with smart contract)
//
// NOT IMPLEMENTED. `solanaConnection` above is constructed and never used,
// because this is where it would have been used. Both are left in place rather
// than culled (rule 9) because deleting them would make the route look
// complete: today /deposit sends GRC and mints nothing, and an empty function
// named wrapGrcToWgrc is the only thing in the file that says so.
async function wrapGrcToWgrc(amount, userAddress) {
  console.log(`Wrapping ${amount} GRC into wGRC for user: ${userAddress}`);
  // Logic for wrapping GRC into wGRC (minting the wGRC token) goes here
  // You should mint the wrapped token here and confirm the transaction
}

app.listen(port, () => {
  console.log(`Server running at http://localhost:${port}`);
  console.log(`Gridcoin RPC URL: ${process.env.GRIDCOIN_RPC_URL}  <- this route SPENDS from the wallet behind this endpoint`);
  console.log('POST /deposit requires the shared secret; it calls sendtoaddress with the amount from the request body and has no ceiling');
});
