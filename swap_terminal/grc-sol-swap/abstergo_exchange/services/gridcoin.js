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
 * Mainnet-safe: yes as of 2026-10-08, and it was NO until that day. The line
 *        here used to read "There is no network selector here at all; whatever
 *        GRIDCOIN_RPC_URL points at is what gets spent from", and that was
 *        true: this was the only money-moving path in the repository with no
 *        network check. handleGridcoinSwap now reads the daemon's network
 *        before every sendtoaddress and refuses anything outside the GRC test
 *        allowlist, including a daemon that names no network. The gate is
 *        ../daemon_network.js and the decision is tested in
 *        tests/daemon_network.test.js; the WIRING is not tested, because that
 *        needs a daemon -- see that file.
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
 * this route claims to do does not exist.
 *
 * THIS PARAGRAPH ENDED WITH "src/GridcoinBalance.jsx:38 posts to it from the
 * browser" UNTIL 2026-10-08, AND THAT FILE NO LONGER EXISTS -- 97c820a deleted
 * the frontend ("One of the two UIs could not build and nothing in the tree
 * referenced it"). Verified by looking: there is no src/ directory here and no
 * file named GridcoinBalance anywhere in the tree.
 *
 * It matters because it was the ONE caller this header could name, and the
 * paragraph below ("WHAT CHANGED") is written around breaking it. With it gone,
 * `/deposit` has no caller of any kind: no import, no fetch, no launcher, no
 * shell script, no compose service -- the image does not even ship this
 * directory. The residual the header hands to the operator is therefore
 * narrower than it was, and it is exactly one thing: somebody running
 * `node services/gridcoin.js` by hand.
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
 * to move funds from unauthenticated callers and adds none. It did break
 * src/GridcoinBalance.jsx, which had no secret to present -- deliberately, and
 * said here rather than discovered later: a browser button that spends from the
 * hot wallet with no authentication is the defect, not the thing to preserve.
 * (That file has since been deleted outright; see above.)
 *
 * WHAT CHANGED 2026-10-08: THE NETWORK GATE. handleGridcoinSwap reads the
 * daemon's network -- getblockchaininfo.chain, falling back to getinfo.testnet
 * -- and refuses the send unless the answer is in the GRC test allowlist. An
 * unreadable network refuses too; it is not read as "probably testnet". Like
 * the shared secret, this only ever REMOVES an ability to spend, and the
 * refusal returns 403 with its reason rather than a bare 500, because "this
 * daemon is not one we spend from" and "this route is broken" are different
 * answers and retrying helps with only one of them.
 *
 * The gate is a named duplicate of swap_terminal/chains/daemon_network.py and
 * that file is named at both sites per rule 8. Nothing else in this file's
 * behavior was touched.
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
import { isNamed, networkFromRpcAnswers, spendVerdict } from '../daemon_network.js';

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
    // A NETWORK REFUSAL IS NOT AN INTERNAL ERROR, and before the gate existed
    // there was nothing here that could tell the two apart. 500 says "this
    // route is broken, retry later"; a refusal says "this daemon is not one
    // this repository will spend from, and retrying changes nothing". Rule 14:
    // did-nothing must not look the same as did-work, and a refusal must name
    // the condition it refused on rather than the operator guessing.
    if (error && error.networkRefusal) {
      console.error('Refusing deposit:', error.message);
      return res.status(403).json({ error: error.message, refused: 'network' });
    }
    // Annotated, not changed (rule 12's BLE001 test). The caller is told 500
    // and the real cause is printed, so a failure cannot be mistaken here for
    // a success. What this DOES hide is whether the send happened: see the
    // note in handleGridcoinSwap.
    console.error('Error handling deposit:', error);
    return res.status(500).json({ error: 'Internal Server Error' });
  }
});

/**
 * One Gridcoin JSON-RPC call. Returns the result, or throws.
 *
 * Extracted so the network probe and the send use ONE transport rather than
 * two spellings of axios.post. There were two reasons not to leave the probe
 * inline: a probe that builds its own request can drift from the request it is
 * meant to be vouching for (different URL source, different timeout), and a
 * reader has to check both to know what the gate actually tested.
 */
async function gridcoinRpc(method, params = []) {
  const response = await axios.post(process.env.GRIDCOIN_RPC_URL, {
    jsonrpc: '2.0',
    method,
    params,
    id: 1,
  });
  if (response.data.error) {
    const error = new Error(`${method}: ${response.data.error.message || 'RPC error'}`);
    error.rpcCode = response.data.error.code;
    throw error;
  }
  return response.data.result;
}

/**
 * Which network GRIDCOIN_RPC_URL points at, asked the way the Python senders ask.
 *
 * Both routes are tried and NEITHER failure is thrown: an older daemon has no
 * getblockchaininfo and answers "Method not found", so a throw there is the
 * signal to try getinfo, not an error. The errors are handed to
 * networkFromRpcAnswers, which folds them into the "unknown" string so an
 * operator sees WHY the network could not be read rather than only that it
 * could not. Whatever comes back, spendVerdict() refuses anything that is not
 * in the GRC test allowlist -- including "unknown".
 */
async function readDaemonNetwork() {
  const answers = {};
  for (const [method, key] of [['getblockchaininfo', 'blockchainInfo'], ['getinfo', 'info']]) {
    try {
      answers[key] = await gridcoinRpc(method);
    } catch (error) {
      // noqa-equivalent: caught BROADLY and the caller CAN tell the failure
      // from a real answer, because the Error object itself is what gets
      // passed on and networkFromRpcAnswers names it in the refusal. This is
      // the one case CLAUDE.md rule 12 allows a broad catch -- a probe that
      // must not die on one unsupported method -- and the handler says so in
      // its return value exactly as that rule requires.
      answers[key] = error instanceof Error ? error : new Error(String(error));
    }
  }
  return networkFromRpcAnswers(answers);
}

// Handle Gridcoin deposit transfer to exchange wallet
async function handleGridcoinSwap(grcAmount, exchangeWalletAddress) {
  // THE NETWORK GATE, AND IT RUNS BEFORE EVERY SEND RATHER THAN ONCE AT
  // STARTUP. Added 2026-10-08. GRIDCOIN_RPC_URL is read from the environment at
  // call time by gridcoinRpc above, so a verdict cached at boot would be a
  // verdict about a URL this call is not necessarily using. One extra RPC
  // round trip on a path that is about to spend money is not a cost worth
  // optimizing, and rule 13's "verify the artifact, not the deploy" is the
  // same argument: check the daemon you are about to send through.
  //
  // WHY THIS FILE AND WHY NOW. Every Gridcoin send on the Python side goes
  // through swap_terminal/chains/daemon_network.py and refuses a daemon whose
  // network is not in an allowlist. This route asked nothing: measured
  // 2026-10-08, it was the only money-moving path in the repository with no
  // network check of any kind, which its own header admitted in the words
  // "Mainnet-safe: NO. There is no network selector here at all". The gate is
  // in ../daemon_network.js, it is a pure function, and the 11 assertions in
  // tests/daemon_network.test.js are the only part of this that could be
  // tested without a daemon -- see that file's note on what is NOT covered.
  const network = await readDaemonNetwork();
  const verdict = spendVerdict(network);
  if (!verdict.allowed) {
    // Refuse LOUDLY and name the condition. The amount and destination are
    // printed because an operator reading this line needs to know which send
    // was stopped; the RPC URL is not, because it carries credentials in
    // userinfo form on this daemon's usual spelling.
    console.error(
      `REFUSED: sendtoaddress ${grcAmount} GRC to ${exchangeWalletAddress} -- ${verdict.reason}`,
    );
    const refusal = new Error(`refusing to spend on network=${network}: ${verdict.reason}`);
    refusal.networkRefusal = true;
    throw refusal;
  }
  console.log(
    `network=${network} allowed; sending ${grcAmount} GRC to ${exchangeWalletAddress}`,
  );

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

app.listen(port, async () => {
  console.log(`Server running at http://localhost:${port}`);
  console.log(`Gridcoin RPC URL: ${process.env.GRIDCOIN_RPC_URL}  <- this route SPENDS from the wallet behind this endpoint`);
  console.log('POST /deposit requires the shared secret; it calls sendtoaddress with the amount from the request body and has no ceiling');

  // ANNOUNCE THE POSTURE AT STARTUP, not only at the moment of a refusal.
  // Rule 14: an operator who starts this by hand -- the one way it can be
  // started at all -- should be able to read off the screen whether a send
  // would be permitted, instead of discovering it by sending. This probe is
  // ADVISORY and nothing branches on it: handleGridcoinSwap re-reads the
  // network before every send, because GRIDCOIN_RPC_URL is environment and can
  // name a different daemon by then.
  const network = await readDaemonNetwork();
  const verdict = spendVerdict(network);
  if (!isNamed(network)) {
    console.log(`network: ${network}  <- a send would be REFUSED; the daemon named no network`);
  } else {
    console.log(
      `network: ${network}  <- a send would be ${verdict.allowed ? 'ALLOWED' : 'REFUSED'}: ${verdict.reason}`,
    );
  }
});
