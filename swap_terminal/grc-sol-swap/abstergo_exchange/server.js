/**
 * The Solana bridge: verify a Gridcoin deposit, pay the matching SOL out.
 *
 * Role: file (entry point -- `node server.js`) plus its own route layer
 * Reads: the process environment, swap_intents.json (through intent_store.js),
 *        the Gridcoin daemon over JSON-RPC, the Solana RPC endpoint, the price
 *        feed at VITE_API_URL, and the payer keypair at
 *        SOLANA_PAYER_KEYPAIR_PATH if one is configured
 * Writes: swap_intents.json (through intent_store.js) AND THE SOLANA CHAIN
 * Can move funds: YES. `sendSolPayout()` signs with the payer keypair and calls
 *        sendAndConfirmTransaction. It is the only broadcast in this suite and
 *        it is final the instant the cluster accepts it.
 * Mainnet-safe: NO. DEVNET_RPC_URL is a variable NAME, not a guarantee -- point
 *        it at a mainnet endpoint and this server pays mainnet SOL. Running it
 *        with a funded payer keypair is operating the payout path, not
 *        inspecting it.
 *
 * THE ROUTE TABLE, and what each route requires. Kept here because the answer
 * to "which routes are open?" should be readable in one place rather than
 * assembled by grepping nine handlers (rule 14: pasted output has to be
 * self-describing).
 *
 *   GET  /health                            open (minimal) / secret (detailed)
 *   GET  /prices                            open      -- public price feed proxy
 *   GET  /deposit-addresses                 open      -- public addresses only
 *   GET  /swap-intents/:intentId            SECRET    <- was open
 *   POST /quote/grc-to-sol                  open      -- pure function, no state
 *   POST /swap-intents                      open      -- depositor entry point
 *   POST /swap-intents/:id/verify-gridcoin  SECRET    -- was already
 *   POST /swap-intents/:id/execute          SECRET    <- WAS OPEN, AND IT PAYS
 *
 * The reasoning for each is at its handler, including for the ones that stay
 * open: "why is this not authenticated" is a question that gets asked once per
 * reader and should be answered next to the code rather than re-derived.
 *
 * The second Express server in this directory, services/gridcoin.js, is not
 * part of this file and carries its own header. It is also not started by
 * anything -- see the note there.
 */

import express from 'express';
import cors from 'cors';
import axios from 'axios';
import dotenv from 'dotenv';
import fs from 'fs';
import path from 'path';
import crypto from 'crypto';
import {
  SHARED_SECRET_HEADER,
  describeSharedSecretConfig,
  hasValidSharedSecret,
  requireSharedSecret as assertSharedSecret,
} from './auth.js';
import {
  claimIntentForPayout,
  createIntent,
  formatDurationMs,
  getIntentById,
  recordPayoutFailure,
  recordPayoutSuccess,
  updateIntent,
} from './intent_store.js';
import {
  Connection,
  PublicKey,
  Keypair,
  Transaction,
  SystemProgram,
  LAMPORTS_PER_SOL,
  sendAndConfirmTransaction,
} from '@solana/web3.js';

dotenv.config();

const DEVNET_RPC_URL = process.env.DEVNET_RPC_URL;
const API_URL = process.env.VITE_API_URL;
const GRIDCOIN_DEPOSIT_ADDRESS = process.env.GRIDCOIN_DEPOSIT_ADDRESS || process.env.EXCHANGE_WALLET_ADDRESS;
const SOLANA_HOT_WALLET_PUBLIC_KEY = process.env.SOLANA_HOT_WALLET_PUBLIC_KEY || null;
const SOLANA_PAYER_KEYPAIR_PATH = process.env.SOLANA_PAYER_KEYPAIR_PATH || null;
const PORT = Number(process.env.PORT || 5000);

const PRICE_CACHE_TTL_MS = Number(process.env.PRICE_CACHE_TTL_MS || 30000);
const SWAP_INTENTS_PATH = process.env.SWAP_INTENTS_PATH || path.resolve(process.cwd(), 'swap_intents.json');
const GRIDCOIN_VERIFY_SHARED_SECRET = process.env.GRIDCOIN_VERIFY_SHARED_SECRET || null;
const REQUIRE_GRIDCOIN_SHARED_SECRET =
  String(process.env.REQUIRE_GRIDCOIN_SHARED_SECRET || 'true').toLowerCase() !== 'false';
const INTENT_TTL_MS = Number(process.env.SWAP_INTENT_TTL_MS || 60 * 60 * 1000);
const GRIDCOIN_MIN_CONFIRMATIONS = Number(process.env.GRIDCOIN_MIN_CONFIRMATIONS || 6);

const GRIDCOIN_RPC_URL =
  process.env.GRIDCOIN_RPC_URL ||
  (process.env.GRIDCOIN_RPC_HOST && process.env.GRIDCOIN_RPC_PORT
    ? `http://${process.env.GRIDCOIN_RPC_HOST}:${process.env.GRIDCOIN_RPC_PORT}`
    : process.env.RPC_URL || null);

const GRIDCOIN_RPC_USER = process.env.GRIDCOIN_RPC_USER || process.env.RPC_USER || null;
const GRIDCOIN_RPC_PASSWORD = process.env.GRIDCOIN_RPC_PASSWORD || process.env.RPC_PASS || null;

if (!DEVNET_RPC_URL) throw new Error('DEVNET_RPC_URL is required');
if (!API_URL) throw new Error('VITE_API_URL is required');
if (!GRIDCOIN_DEPOSIT_ADDRESS) throw new Error('GRIDCOIN_DEPOSIT_ADDRESS or EXCHANGE_WALLET_ADDRESS is required');
if (!GRIDCOIN_RPC_URL) throw new Error('GRIDCOIN_RPC_URL or GRIDCOIN_RPC_HOST/GRIDCOIN_RPC_PORT is required');
if (!GRIDCOIN_RPC_USER || !GRIDCOIN_RPC_PASSWORD) throw new Error('Gridcoin RPC credentials are required');

const app = express();
const solanaConnection = new Connection(DEVNET_RPC_URL, 'confirmed');
let priceCache = { data: null, expiresAt: 0 };

app.use(cors());
app.use(express.json());

function parsePositiveNumber(value, label) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    throw new Error(`${label} must be a positive number`);
  }
  return parsed;
}

function parseOptionalNonNegativeNumber(value, label) {
  if (value === undefined || value === null || value === '') return null;
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed < 0) {
    throw new Error(`${label} must be a non-negative number`);
  }
  return parsed;
}

function parsePublicKey(value, label) {
  try {
    return new PublicKey(value);
  } catch {
    throw new Error(`${label} is not a valid Solana public key`);
  }
}

function loadKeypair(filePath) {
  const raw = fs.readFileSync(filePath, 'utf8');
  const secret = JSON.parse(raw);
  return Keypair.fromSecretKey(new Uint8Array(secret));
}

const configuredPayer = SOLANA_PAYER_KEYPAIR_PATH ? loadKeypair(SOLANA_PAYER_KEYPAIR_PATH) : null;
const derivedHotWalletPublicKey = configuredPayer ? configuredPayer.publicKey.toBase58() : null;

if (SOLANA_HOT_WALLET_PUBLIC_KEY && derivedHotWalletPublicKey && SOLANA_HOT_WALLET_PUBLIC_KEY !== derivedHotWalletPublicKey) {
  throw new Error(
    `SOLANA_HOT_WALLET_PUBLIC_KEY (${SOLANA_HOT_WALLET_PUBLIC_KEY}) does not match payer keypair (${derivedHotWalletPublicKey})`,
  );
}

const EFFECTIVE_SOLANA_HOT_WALLET_PUBLIC_KEY = derivedHotWalletPublicKey || SOLANA_HOT_WALLET_PUBLIC_KEY;

/**
 * The shared-secret configuration, resolved once and passed to auth.js.
 *
 * auth.js deliberately does not read process.env itself (rule 12: no
 * import-time side effects, and a decision that takes its inputs as arguments
 * is a decision that can be tested with seeded ones). This object is that
 * argument.
 */
const SHARED_SECRET_CONFIG = {
  enforcementEnabled: REQUIRE_GRIDCOIN_SHARED_SECRET,
  secret: GRIDCOIN_VERIFY_SHARED_SECRET,
};

/**
 * Throw a 401 unless the request carries the shared secret.
 *
 * Kept as a one-line wrapper so every route reads `requireSharedSecret(req)`
 * exactly as it did before, while the decision itself lives in auth.js where a
 * test can call it with seeded inputs.
 */
function requireSharedSecret(req) {
  assertSharedSecret(req, SHARED_SECRET_CONFIG);
}

/**
 * FAIL-CLOSED STARTUP CHECK -- runs at import, before app.listen.
 *
 * Measured 2026-09-24: REQUIRE_GRIDCOIN_SHARED_SECRET could be set to 'false'
 * and the server would start, serve, and pay out, with every route open. The
 * DEFAULT was already safe (enforcement is disabled only by the exact string
 * 'false', so unset, empty, '0' and 'no' all enforce -- fail-closed by
 * construction, unlike the `Boolean(process.env.X)` spelling of the same idea,
 * which fails open on an unset variable). What was missing was any check on
 * the combination of that switch with an armed payer keypair. See auth.js's
 * describeSharedSecretConfig for why that one combination is fatal rather than
 * a warning.
 */
const SHARED_SECRET_STATUS = describeSharedSecretConfig({
  enforcementEnabled: REQUIRE_GRIDCOIN_SHARED_SECRET,
  secret: GRIDCOIN_VERIFY_SHARED_SECRET,
  payoutEnabled: Boolean(configuredPayer),
});

for (const line of SHARED_SECRET_STATUS.lines) console.log(line);
if (SHARED_SECRET_STATUS.fatal) throw new Error(SHARED_SECRET_STATUS.fatal);

function newIntentId() {
  return `si_${crypto.randomBytes(12).toString('hex')}`;
}

function nowIso() {
  return new Date().toISOString();
}

function isIntentExpired(intent) {
  return Date.now() > new Date(intent.expiresAt).getTime();
}

async function getPrices() {
  const now = Date.now();
  if (priceCache.data && priceCache.expiresAt > now) return priceCache.data;
  const response = await axios.get(API_URL, { timeout: 10000 });
  priceCache = { data: response.data, expiresAt: now + PRICE_CACHE_TTL_MS };
  return response.data;
}

async function quoteGrcToSol(grcAmount) {
  const prices = await getPrices();
  const grcPriceUsd = Number(prices['gridcoin-research']?.usd || 0);
  const solPriceUsd = Number(prices['solana']?.usd || 0);

  if (grcPriceUsd <= 0 || solPriceUsd <= 0) {
    throw new Error('Pricing feed returned zero or missing price data');
  }

  const grossSol = (grcAmount * grcPriceUsd) / solPriceUsd;
  const lamports = Math.floor(grossSol * LAMPORTS_PER_SOL);

  if (lamports <= 0) throw new Error('Quoted amount rounds down to zero lamports');

  return {
    grcAmount,
    grcPriceUsd,
    solPriceUsd,
    solAmount: lamports / LAMPORTS_PER_SOL,
    lamports,
  };
}

async function sendSolPayout(destinationAddress, lamports) {
  if (!configuredPayer) throw new Error('SOLANA_PAYER_KEYPAIR_PATH is not configured');
  const destination = parsePublicKey(destinationAddress, 'destinationSolanaAddress');

  const tx = new Transaction().add(
    SystemProgram.transfer({
      fromPubkey: configuredPayer.publicKey,
      toPubkey: destination,
      lamports,
    }),
  );

  return sendAndConfirmTransaction(solanaConnection, tx, [configuredPayer]);
}

async function gridcoinRpc(method, params = []) {
  const response = await axios.post(
    GRIDCOIN_RPC_URL,
    {
      jsonrpc: '1.0',
      id: `grc-${Date.now()}`,
      method,
      params,
    },
    {
      timeout: 15000,
      auth: {
        username: GRIDCOIN_RPC_USER,
        password: GRIDCOIN_RPC_PASSWORD,
      },
    },
  );

  if (response.data?.error) {
    throw new Error(`Gridcoin RPC ${method} failed: ${response.data.error.message || JSON.stringify(response.data.error)}`);
  }

  return response.data.result;
}

function sumOutputsToAddress(vout, targetAddress) {
  let total = 0;
  let matched = false;

  for (const out of Array.isArray(vout) ? vout : []) {
    const addresses = out?.scriptPubKey?.addresses || [];
    if (addresses.includes(targetAddress)) {
      total += Number(out.value || 0);
      matched = true;
    }
  }

  return { total, matched };
}

function extractWalletAwareCredit(tx, targetAddress) {
  let total = 0;
  let matched = false;

  if (Array.isArray(tx?.details)) {
    for (const detail of tx.details) {
      if (detail?.address === targetAddress && Number(detail?.amount || 0) > 0) {
        total += Number(detail.amount);
        matched = true;
      }
    }
  }

  return { total, matched };
}

async function lookupGridcoinTransaction(txid) {
  try {
    const walletTx = await gridcoinRpc('gettransaction', [txid]);
    const decoded = walletTx?.hex ? await gridcoinRpc('decoderawtransaction', [walletTx.hex]) : null;
    return {
      source: 'gettransaction',
      walletTx,
      decoded,
    };
  } catch (walletErr) {
    try {
      const rawVerbose = await gridcoinRpc('getrawtransaction', [txid, true]);
      return {
        source: 'getrawtransaction',
        walletTx: null,
        decoded: rawVerbose,
      };
    } catch (rawErr) {
      throw new Error(`Unable to lookup Gridcoin tx ${txid}: ${walletErr.message}; fallback failed: ${rawErr.message}`);
    }
  }
}

async function verifyGridcoinReceiptForIntent(intent, gridcoinTxid) {
  const lookup = await lookupGridcoinTransaction(gridcoinTxid);
  const walletTx = lookup.walletTx;
  const decoded = lookup.decoded;

  const confirmations =
    parseOptionalNonNegativeNumber(walletTx?.confirmations, 'confirmations') ??
    parseOptionalNonNegativeNumber(decoded?.confirmations, 'confirmations') ??
    0;

  if (confirmations < GRIDCOIN_MIN_CONFIRMATIONS) {
    throw new Error(`Gridcoin tx has ${confirmations} confirmations, requires at least ${GRIDCOIN_MIN_CONFIRMATIONS}`);
  }

  const walletCredit = extractWalletAwareCredit(walletTx, intent.gridcoinDepositAddress);
  const decodedCredit = sumOutputsToAddress(decoded?.vout, intent.gridcoinDepositAddress);

  const matched = walletCredit.matched || decodedCredit.matched;
  const receivedGrcAmount = Math.max(walletCredit.total, decodedCredit.total);

  if (!matched) {
    throw new Error(`Gridcoin tx does not pay the configured deposit address ${intent.gridcoinDepositAddress}`);
  }

  if (receivedGrcAmount < Number(intent.expectedGrcAmount)) {
    throw new Error(
      `Gridcoin tx amount ${receivedGrcAmount} is below expected ${intent.expectedGrcAmount}`,
    );
  }

  return {
    gridcoinTxid,
    confirmations,
    receivedGrcAmount,
    sourceGridcoinAddress: null,
    verifiedAt: nowIso(),
    verificationSource: lookup.source,
  };
}

/**
 * Liveness, plus diagnostics to an authenticated caller.
 *
 * AUTHENTICATION: partially required, and the split is the point.
 *
 * Before 2026-09-24 this route was fully open and answered with the Gridcoin
 * RPC URL, the hot-wallet public key, the absolute path of the intent store,
 * and whether payouts were armed. That is a map of the deployment handed to
 * anyone who can reach the port -- not a key and not money, but every one of
 * those is a fact an attacker would otherwise have to guess, and
 * `payoutEnabled: true` in particular says "this host will sign transfers."
 *
 * Making the whole route authenticated would have broken the legitimate use:
 * an unauthenticated liveness probe (a container health check, an uptime
 * monitor) has no secret to present and only needs to know the process is up.
 * So the probe still gets an answer, and the answer carries nothing that is
 * not already implied by the port being open.
 */
app.get('/health', async (req, res) => {
  const detailed = hasValidSharedSecret(req, SHARED_SECRET_CONFIG);
  try {
    const version = await solanaConnection.getVersion();
    if (!detailed) {
      // Rule 14: an empty or ambiguous result is a defect. This says what it
      // is and why it is short, so an operator who expected the full body can
      // tell "misconfigured" from "unauthenticated" without reading the source.
      return res.json({
        ok: true,
        detail: `omitted; present the ${SHARED_SECRET_HEADER} header for the full diagnostic body`,
      });
    }
    res.json({
      ok: true,
      rpcUrl: DEVNET_RPC_URL,
      solanaCore: version['solana-core'],
      gridcoinDepositAddress: GRIDCOIN_DEPOSIT_ADDRESS,
      solanaHotWallet: EFFECTIVE_SOLANA_HOT_WALLET_PUBLIC_KEY,
      payoutEnabled: Boolean(configuredPayer),
      intentStorePath: SWAP_INTENTS_PATH,
      verificationSecretRequired: REQUIRE_GRIDCOIN_SHARED_SECRET,
      gridcoinRpcUrl: GRIDCOIN_RPC_URL,
      gridcoinMinConfirmations: GRIDCOIN_MIN_CONFIRMATIONS,
    });
  } catch (error) {
    // The Solana RPC being unreachable is a real 500 and is reported as one.
    // The message is echoed only to an authenticated caller, because an RPC
    // error string routinely contains the endpoint URL.
    res.status(500).json({ ok: false, error: detailed ? error.message : 'health check failed' });
  }
});

/**
 * AUTHENTICATION: deliberately NOT required, and this is the reasoning, so the
 * next reader does not have to decide it again.
 *
 * It is a cached read-through proxy of a public price feed (CoinGecko). It
 * writes no state, reads no intent, names no address and cannot move funds --
 * and src/App.jsx fetches it directly from the browser, which has nowhere safe
 * to hold a shared secret. Gating it would break the frontend to protect data
 * that is public at its source.
 */
app.get('/prices', async (_req, res) => {
  try {
    res.json(await getPrices());
  } catch (error) {
    res.status(502).json({ error: error.message });
  }
});

/**
 * AUTHENTICATION: deliberately NOT required.
 *
 * Both values are public by construction -- the Gridcoin deposit address is
 * the address a depositor is supposed to send to, and a Solana public key is
 * public. Neither is a credential and neither is a decision. Withholding them
 * would protect nothing and would break the deposit flow.
 */
app.get('/deposit-addresses', (_req, res) => {
  res.json({
    gridcoinDepositAddress: GRIDCOIN_DEPOSIT_ADDRESS,
    solanaHotWallet: EFFECTIVE_SOLANA_HOT_WALLET_PUBLIC_KEY,
  });
});

/**
 * AUTHENTICATION: NOW REQUIRED. It was not before 2026-09-24.
 *
 * This route returns a whole intent: the depositor's destination Solana
 * address, the exact lamport amount, the verified deposit including its
 * Gridcoin txid, and -- once paid -- the payout signature. That is the full
 * transaction history of one customer, handed to anyone who has the intentId.
 *
 * And the intentId is not a secret worth relying on as one. It is 12 random
 * bytes, so it is not guessable, but it is handed to a browser, it travels in
 * a URL (and therefore into access logs and Referer headers), and an
 * unauthenticated GET that echoes the entire record turns one leaked id into
 * a complete disclosure. "Unguessable identifier" is not authentication; it is
 * an identifier.
 *
 * WHO THIS COULD BREAK: no caller in this tree. Measured by grepping every
 * .js/.jsx under grc-sol-swap/ for `swap-intents` -- the only frontend call is
 * SwapIntentForm.jsx's POST to /swap-intents. Nothing polls this route. If a
 * depositor-facing status page is added later, it needs a per-intent token,
 * not a shared operator secret.
 */
app.get('/swap-intents/:intentId', (req, res) => {
  try {
    requireSharedSecret(req);
    const intent = getIntentById(SWAP_INTENTS_PATH, req.params.intentId);
    if (!intent) return res.status(404).json({ success: false, error: 'Intent not found' });
    res.json({ success: true, intent });
  } catch (error) {
    res.status(error.statusCode || 500).json({ success: false, error: error.message });
  }
});

/**
 * AUTHENTICATION: deliberately NOT required.
 *
 * A quote is a pure function of a public price feed and an amount. It writes
 * nothing, reads no intent, and creates no obligation -- the intent created
 * later carries its own quote, and `/execute` pays `intent.expectedQuote`, not
 * anything a quote request returned. Gating it would protect nothing.
 */
app.post('/quote/grc-to-sol', async (req, res) => {
  try {
    const grcAmount = parsePositiveNumber(req.body?.grcAmount, 'grcAmount');
    const quote = await quoteGrcToSol(grcAmount);
    res.json({ success: true, quote });
  } catch (error) {
    res.status(400).json({ success: false, error: error.message });
  }
});

/**
 * AUTHENTICATION: deliberately NOT required, with a caveat recorded below.
 *
 * This is the depositor's entry point and SwapIntentForm.jsx calls it from the
 * browser, which has nowhere to hold a shared secret. An intent created here
 * starts at `awaiting_deposit` and cannot become payable without a
 * `/verify-gridcoin` call, which DOES require the secret -- so creating one
 * releases no funds and commits the operator to nothing.
 *
 * TWO THINGS THIS LEAVES OPEN, named rather than fixed, because both are
 * changes to what the bridge will accept and therefore the operator's (rule
 * 16):
 *
 *   - Unbounded growth. Every POST appends a row to a JSON file that is read
 *     in full on every request. Nothing rate-limits it and nothing prunes
 *     expired intents. A few thousand junk intents make every route slow; a
 *     few million make the process run out of memory parsing its own store.
 *     The migration to SQLite (migrate_swap_intents.py) removes the
 *     read-everything-per-request half of this.
 *
 *   - An intent names the DESTINATION but nothing binds it to a DEPOSITOR.
 *     Anyone can create an intent for 100 GRC paying their own Solana address.
 *     Verification then checks that some Gridcoin transaction paid the shared
 *     deposit address with at least that amount -- it does not check WHO sent
 *     it (verifyGridcoinReceiptForIntent returns sourceGridcoinAddress: null,
 *     hard-coded). An operator verifying a txid against the wrong intent would
 *     pay the wrong person, and the server would not be able to tell. That is
 *     a property of the verification step, not of this route, and it is
 *     surfaced rather than changed.
 */
app.post('/swap-intents', async (req, res) => {
  try {
    const grcAmount = parsePositiveNumber(req.body?.grcAmount, 'grcAmount');
    const destinationSolanaAddress = String(req.body?.destinationSolanaAddress || req.body?.userAddress || '').trim();
    parsePublicKey(destinationSolanaAddress, 'destinationSolanaAddress');

    const quote = await quoteGrcToSol(grcAmount);
    const createdAt = nowIso();
    const expiresAt = new Date(Date.now() + INTENT_TTL_MS).toISOString();

    const intent = {
      intentId: newIntentId(),
      status: 'awaiting_deposit',
      createdAt,
      expiresAt,
      gridcoinDepositAddress: GRIDCOIN_DEPOSIT_ADDRESS,
      destinationSolanaAddress,
      expectedGrcAmount: grcAmount,
      expectedQuote: quote,
      verifiedDeposit: null,
      payout: null,
    };

    await createIntent(SWAP_INTENTS_PATH, intent);
    res.status(201).json({ success: true, intent });
  } catch (error) {
    // A store-level failure (unreadable JSON, lock contention) carries its own
    // statusCode and is NOT a 400: the caller's request was fine. Conflating
    // the two is how "the store is corrupt" gets reported to a depositor as
    // "your amount is invalid".
    res.status(error.statusCode || 400).json({ success: false, error: error.message });
  }
});

/**
 * AUTHENTICATION: required, and it was the ONLY route that checked before
 * 2026-09-24. The check itself was `provided !== SECRET`, a short-circuiting
 * string comparison whose failure time leaks how many leading bytes matched;
 * it is now a constant-time digest comparison in auth.js.
 *
 * This is the step that makes an intent payable, so it is the correct place
 * for a secret: only the operator, who has watched the Gridcoin deposit
 * confirm, can move an intent to `verified`.
 */
app.post('/swap-intents/:intentId/verify-gridcoin', async (req, res) => {
  try {
    requireSharedSecret(req);

    const { intentId } = req.params;
    const gridcoinTxid = String(req.body?.gridcoinTxid || '').trim();
    if (!gridcoinTxid) throw new Error('gridcoinTxid is required');

    const existing = getIntentById(SWAP_INTENTS_PATH, intentId);
    if (!existing) return res.status(404).json({ success: false, error: 'Intent not found' });
    if (existing.status === 'paid') return res.status(409).json({ success: false, error: 'Intent already paid' });
    if (existing.status === 'paying') return res.status(409).json({ success: false, error: 'Intent has a payout in flight' });
    if (existing.status === 'payout_failed') {
      return res.status(409).json({
        success: false,
        error: 'Intent has a failed payout and needs an operator to establish on-chain what happened before it can be re-armed',
      });
    }
    if (existing.status === 'verified') return res.status(409).json({ success: false, error: 'Intent already verified' });
    if (isIntentExpired(existing)) {
      const expired = await updateIntent(SWAP_INTENTS_PATH, intentId, (current) => ({ ...current, status: 'expired' }));
      return res.status(400).json({ success: false, error: 'Intent has expired', intent: expired });
    }

    // The network call happens OUTSIDE the store lock, deliberately. Holding a
    // lock across an RPC round trip to a Gridcoin daemon means one slow or
    // hung daemon stops every other request in the process. The status
    // transition below re-reads under the lock and re-checks, so nothing is
    // decided from the stale copy read above.
    const verifiedDeposit = await verifyGridcoinReceiptForIntent(existing, gridcoinTxid);

    const updated = await updateIntent(SWAP_INTENTS_PATH, intentId, (intent) => {
      if (intent.status !== 'awaiting_deposit') {
        // Re-checked inside the lock: between the read above and here, another
        // request may have verified, paid or expired this intent. Overwriting
        // a `paying` or `paid` intent with `verified` would re-arm a payout.
        const err = new Error(`Intent moved to ${intent.status} while its deposit was being verified; refusing to overwrite`);
        err.statusCode = 409;
        throw err;
      }
      return { ...intent, status: 'verified', verifiedDeposit };
    });

    res.json({ success: true, intent: updated });
  } catch (error) {
    res.status(error.statusCode || 400).json({ success: false, error: error.message });
  }
});

/**
 * AUTHENTICATION: NOW REQUIRED. IT HAD NONE.
 *
 * This is the route that signs and broadcasts a Solana transfer out of the hot
 * wallet, and before 2026-09-24 it was the only fund-moving route in either of
 * the two Express servers here with no check of any kind on the caller. It
 * read the intent store, compared `status === 'verified'`, and called
 * sendSolPayout(). Anyone who could reach the port and produce an intentId
 * could trigger a final, irreversible transfer.
 *
 * WHAT ELSE CHANGED HERE, AND WHY EACH PIECE IS LOAD-BEARING.
 *
 * The old handler was check-then-act across an await:
 *
 *     intent = getIntentById(id)            // read the file
 *     if (intent.status !== 'verified') ...  // decide
 *     updateIntent(id, -> 'paying')          // write the file
 *     await sendSolPayout(...)               // spend
 *
 * Two concurrent requests for one intentId interleave at the awaits and both
 * reach sendSolPayout. Node being single-threaded does not prevent it: the
 * event loop runs the other handler at every await point. `claimIntentForPayout`
 * collapses the read, the decision and the durable write into one critical
 * section held by both an in-process mutex and a cross-process lock, and
 * returns a boolean that IS the authorization. Nothing below re-derives it.
 *
 * This is the same defect and the same fix as the Python payout worker on the
 * other side of this repository -- services/payout_service.py, measured in
 * tests/test_payout_concurrency.py, where two workers pay one swap through a
 * guard that is a read. Named at both sites (rule 8) so that whoever finds one
 * is told the other exists.
 *
 * The failure path no longer rolls `paying` back to `verified`. See
 * intent_store.js defect 3: an error out of sendAndConfirmTransaction includes
 * confirmation timeouts on transactions that were broadcast and may have
 * landed, and re-arming on one of those is how one deposit becomes two
 * transfers.
 */
app.post('/swap-intents/:intentId/execute', async (req, res) => {
  const startedAt = Date.now();
  let claim = null;

  try {
    requireSharedSecret(req);
    const { intentId } = req.params;

    if (!configuredPayer) {
      return res.status(501).json({
        success: false,
        error: 'Swap execution is disabled until SOLANA_PAYER_KEYPAIR_PATH is configured',
      });
    }

    // ONE call, and its answer is the whole authorization. Expiry, status,
    // the presence of a verified deposit and the transition to `paying` all
    // happen inside the store lock and are fsynced before this returns.
    claim = await claimIntentForPayout(SWAP_INTENTS_PATH, intentId);

    if (!claim.claimed) {
      // Every refusal reports WHICH one it is, next to the status that caused
      // it (rule 14). A bare 409 taught an operator nothing about whether to
      // wait, look at the chain, or create a new intent.
      const refusals = {
        not_found: [404, 'Intent not found'],
        expired: [400, 'Intent has expired'],
        verified_without_deposit: [
          500,
          'Intent is marked verified but carries no verified deposit; the store is inconsistent and this was not written by this server',
        ],
        not_claimable: [
          409,
          `Intent is not claimable from status '${claim.status}'  <- payable only from 'verified'. ` +
            `'paying' means a payout is already in flight or a process died holding the claim; ` +
            `'paid' means it is done; ` +
            `'payout_failed' means an earlier attempt errored and an operator must establish on-chain what happened before re-arming.`,
        ],
      };
      const [status, message] = refusals[claim.reason] || [409, `Intent refused: ${claim.reason}`];
      return res.status(status).json({ success: false, error: message, intent: claim.intent });
    }

    const intent = claim.intent;
    const signature = await sendSolPayout(intent.destinationSolanaAddress, intent.expectedQuote.lamports);

    const paidIntent = await recordPayoutSuccess(SWAP_INTENTS_PATH, intentId, claim.claimToken, {
      signature,
      lamports: intent.expectedQuote.lamports,
      solAmount: intent.expectedQuote.solAmount,
    });

    console.log(
      `payout sent intent=${intentId} lamports=${intent.expectedQuote.lamports} signature=${signature} in ${formatDurationMs(Date.now() - startedAt)}`,
    );
    res.json({ success: true, intent: paidIntent, signature });
  } catch (error) {
    if (claim?.claimed) {
      // The claim is ours and the send failed or its outcome is unknown. Mark
      // it terminally failed so nothing claims it again, and say so loudly:
      // this line is the operator's only prompt to go and look at the chain.
      console.error(
        `payout FAILED intent=${req.params.intentId} after ${formatDurationMs(Date.now() - startedAt)}: ${error.message}  <- ` +
          `the transfer may still have been broadcast; check the hot wallet on chain before re-arming this intent`,
      );
      try {
        await recordPayoutFailure(SWAP_INTENTS_PATH, req.params.intentId, claim.claimToken, error.message);
      } catch (recordError) {
        // Checked, and NOT swallowed silently (rule 12). If the store cannot
        // record the failure, the intent is left at `paying` -- which is
        // still safe, because `paying` is not claimable -- but the reason is
        // lost unless it is printed here. The original error is what the
        // caller is told; this one is what the operator needs.
        console.error(
          `payout failure could NOT be recorded for intent=${req.params.intentId}: ${recordError.message}  <- ` +
            `intent remains 'paying' and is not claimable, which is safe, but the store does not say why`,
        );
      }
    }
    res.status(error.statusCode || 400).json({ success: false, error: error.message });
  }
});

/**
 * Startup banner.
 *
 * Rule 14: announce before, not only after, and echo the parameters that
 * decide the answer. An operator reading a pasted banner a day later has to be
 * able to tell which store, which Gridcoin daemon, which Solana endpoint and
 * whether the thing was armed -- without opening the source or the .env.
 *
 * What is deliberately NOT printed: the shared secret, the keypair path's
 * CONTENTS, and any credential. auth.js prints the secret's LENGTH, which is
 * what lets a truncated value be spotted without reading it back.
 */
app.listen(PORT, () => {
  console.log(`Server running on http://localhost:${PORT}`);
  console.log(`Effective hot wallet: ${EFFECTIVE_SOLANA_HOT_WALLET_PUBLIC_KEY || 'not configured'}`);
  console.log(`Payout enabled: ${Boolean(configuredPayer)}  <- true means this process will sign and broadcast SOL transfers`);
  console.log(`Swap intent store: ${SWAP_INTENTS_PATH}`);
  console.log(`Solana RPC: ${DEVNET_RPC_URL}  <- the variable is named DEVNET_RPC_URL; the URL is what decides the network`);
  console.log(`Gridcoin RPC URL: ${GRIDCOIN_RPC_URL}`);
  console.log(`Gridcoin min confirmations: ${GRIDCOIN_MIN_CONFIRMATIONS}  <- not a duration; never rendered in microfortnights (rule 6)`);
  console.log('Routes requiring the shared secret: GET /swap-intents/:id, POST /swap-intents/:id/verify-gridcoin, POST /swap-intents/:id/execute');
  console.log('Routes open: GET /health (minimal body), GET /prices, GET /deposit-addresses, POST /quote/grc-to-sol, POST /swap-intents');
});
