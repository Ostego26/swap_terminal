import express from 'express';
import cors from 'cors';
import axios from 'axios';
import dotenv from 'dotenv';
import fs from 'fs';
import path from 'path';
import crypto from 'crypto';
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

function ensureIntentStore() {
  if (!fs.existsSync(SWAP_INTENTS_PATH)) {
    fs.writeFileSync(SWAP_INTENTS_PATH, JSON.stringify({ intents: [] }, null, 2));
  }
}

function loadIntentStore() {
  ensureIntentStore();
  try {
    const parsed = JSON.parse(fs.readFileSync(SWAP_INTENTS_PATH, 'utf8'));
    if (!parsed || !Array.isArray(parsed.intents)) return { intents: [] };
    return parsed;
  } catch {
    return { intents: [] };
  }
}

function saveIntentStore(store) {
  fs.writeFileSync(SWAP_INTENTS_PATH, JSON.stringify(store, null, 2));
}

function listIntents() {
  return loadIntentStore().intents;
}

function getIntentById(intentId) {
  return listIntents().find((intent) => intent.intentId === intentId) || null;
}

function persistNewIntent(intent) {
  const store = loadIntentStore();
  store.intents.push(intent);
  saveIntentStore(store);
  return intent;
}

function updateIntent(intentId, updater) {
  const store = loadIntentStore();
  const index = store.intents.findIndex((intent) => intent.intentId === intentId);
  if (index === -1) throw new Error(`Unknown intentId: ${intentId}`);
  const current = store.intents[index];
  const next = updater({ ...current });
  store.intents[index] = next;
  saveIntentStore(store);
  return next;
}

function newIntentId() {
  return `si_${crypto.randomBytes(12).toString('hex')}`;
}

function nowIso() {
  return new Date().toISOString();
}

function isIntentExpired(intent) {
  return Date.now() > new Date(intent.expiresAt).getTime();
}

function requireSharedSecret(req) {
  if (!REQUIRE_GRIDCOIN_SHARED_SECRET) return;
  if (!GRIDCOIN_VERIFY_SHARED_SECRET) {
    throw new Error('GRIDCOIN_VERIFY_SHARED_SECRET is required when verification secret enforcement is enabled');
  }
  const provided = req.get('x-gridcoin-verify-secret');
  if (provided !== GRIDCOIN_VERIFY_SHARED_SECRET) {
    const err = new Error('Unauthorized Gridcoin verification request');
    err.statusCode = 401;
    throw err;
  }
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

app.get('/health', async (_req, res) => {
  try {
    const version = await solanaConnection.getVersion();
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
    res.status(500).json({ ok: false, error: error.message });
  }
});

app.get('/prices', async (_req, res) => {
  try {
    res.json(await getPrices());
  } catch (error) {
    res.status(502).json({ error: error.message });
  }
});

app.get('/deposit-addresses', (_req, res) => {
  res.json({
    gridcoinDepositAddress: GRIDCOIN_DEPOSIT_ADDRESS,
    solanaHotWallet: EFFECTIVE_SOLANA_HOT_WALLET_PUBLIC_KEY,
  });
});

app.get('/swap-intents/:intentId', (req, res) => {
  const intent = getIntentById(req.params.intentId);
  if (!intent) return res.status(404).json({ success: false, error: 'Intent not found' });
  res.json({ success: true, intent });
});

app.post('/quote/grc-to-sol', async (req, res) => {
  try {
    const grcAmount = parsePositiveNumber(req.body?.grcAmount, 'grcAmount');
    const quote = await quoteGrcToSol(grcAmount);
    res.json({ success: true, quote });
  } catch (error) {
    res.status(400).json({ success: false, error: error.message });
  }
});

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

    persistNewIntent(intent);
    res.status(201).json({ success: true, intent });
  } catch (error) {
    res.status(400).json({ success: false, error: error.message });
  }
});

app.post('/swap-intents/:intentId/verify-gridcoin', async (req, res) => {
  try {
    requireSharedSecret(req);

    const { intentId } = req.params;
    const gridcoinTxid = String(req.body?.gridcoinTxid || '').trim();
    if (!gridcoinTxid) throw new Error('gridcoinTxid is required');

    const existing = getIntentById(intentId);
    if (!existing) return res.status(404).json({ success: false, error: 'Intent not found' });
    if (existing.status === 'paid') return res.status(409).json({ success: false, error: 'Intent already paid' });
    if (existing.status === 'verified') return res.status(409).json({ success: false, error: 'Intent already verified' });
    if (isIntentExpired(existing)) {
      const expired = updateIntent(intentId, (current) => ({ ...current, status: 'expired' }));
      return res.status(400).json({ success: false, error: 'Intent has expired', intent: expired });
    }

    const verifiedDeposit = await verifyGridcoinReceiptForIntent(existing, gridcoinTxid);

    const updated = updateIntent(intentId, (intent) => {
      intent.status = 'verified';
      intent.verifiedDeposit = verifiedDeposit;
      return intent;
    });

    res.json({ success: true, intent: updated });
  } catch (error) {
    res.status(error.statusCode || 400).json({ success: false, error: error.message });
  }
});

app.post('/swap-intents/:intentId/execute', async (req, res) => {
  try {
    const { intentId } = req.params;

    if (!configuredPayer) {
      return res.status(501).json({
        success: false,
        error: 'Swap execution is disabled until SOLANA_PAYER_KEYPAIR_PATH is configured',
      });
    }

    let intent = getIntentById(intentId);
    if (!intent) return res.status(404).json({ success: false, error: 'Intent not found' });

    if (isIntentExpired(intent)) {
      intent = updateIntent(intentId, (current) => ({ ...current, status: 'expired' }));
      return res.status(400).json({ success: false, error: 'Intent has expired', intent });
    }

    if (intent.status === 'paid') {
      return res.status(409).json({ success: false, error: 'Intent already spent', intent });
    }

    if (intent.status !== 'verified' || !intent.verifiedDeposit) {
      return res.status(409).json({ success: false, error: 'Intent is not verified yet', intent });
    }

    intent = updateIntent(intentId, (current) => ({
      ...current,
      status: 'paying',
      payout: {
        ...(current.payout || {}),
        startedAt: nowIso(),
      },
    }));

    const signature = await sendSolPayout(intent.destinationSolanaAddress, intent.expectedQuote.lamports);

    const paidIntent = updateIntent(intentId, (current) => ({
      ...current,
      status: 'paid',
      payout: {
        ...(current.payout || {}),
        signature,
        lamports: current.expectedQuote.lamports,
        solAmount: current.expectedQuote.solAmount,
        paidAt: nowIso(),
      },
    }));

    res.json({ success: true, intent: paidIntent, signature });
  } catch (error) {
    try {
      const { intentId } = req.params;
      const existing = getIntentById(intentId);
      if (existing && existing.status === 'paying') {
        updateIntent(intentId, (current) => ({
          ...current,
          status: 'verified',
          payout: {
            ...(current.payout || {}),
            error: error.message,
            failedAt: nowIso(),
          },
        }));
      }
    } catch {}
    res.status(400).json({ success: false, error: error.message });
  }
});

app.listen(PORT, () => {
  console.log(`Server running on http://localhost:${PORT}`);
  console.log(`Effective hot wallet: ${EFFECTIVE_SOLANA_HOT_WALLET_PUBLIC_KEY || 'not configured'}`);
  console.log(`Payout enabled: ${Boolean(configuredPayer)}`);
  console.log(`Swap intent store: ${SWAP_INTENTS_PATH}`);
  console.log(`Gridcoin RPC URL: ${GRIDCOIN_RPC_URL}`);
  console.log(`Gridcoin min confirmations: ${GRIDCOIN_MIN_CONFIRMATIONS}`);
});
