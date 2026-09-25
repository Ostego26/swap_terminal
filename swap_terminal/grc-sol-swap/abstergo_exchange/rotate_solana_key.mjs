#!/usr/bin/env node
/**
 * Role: operator recovery tool (reports by default; moves funds only with --move)
 * Reads: the compromised keypair file, and the Solana cluster named by --network
 * Writes: a NEW keypair outside the repository, and with --move, transactions
 * Can move funds: YES, and only when --move is passed
 * Mainnet-safe: refuses mainnet-beta unless --i-understand-this-is-mainnet is also passed
 *
 * WHY THIS EXISTS
 *
 * swap_terminal/grc-sol-swap/abstergo_exchange/wgrc.json is an ed25519 keypair
 * that was committed to a git repository and pushed. Its public key is
 * BGdUSPGWiwStabibSXwiLwJCsk6iXDTeyL6fgWbNcAvN, which is the
 * destinationSolanaAddress on all three intents in swap_intents.json -- one of
 * them already marked paid for 5,560,821 lamports.
 *
 * A key that has been pushed is published. Untracking the file and adding a
 * .gitignore rule stops the NEXT commit, not the last one: the key is in the
 * repository's history, in every clone of it, and on GitHub. The only step that
 * changes anything is moving what it controls to a key that was never in a
 * repository. That is what this does.
 *
 * THE ORDER IS THE WHOLE POINT
 *
 * SPL tokens (wGRC among them) do not live in the wallet account. They live in
 * separate token accounts the wallet OWNS, and a plain SOL transfer does not
 * move them. Worse, sending a token requires the destination's Associated Token
 * Account to exist, and creating one costs rent -- paid in SOL, by this wallet,
 * at the time of the transfer.
 *
 * So: tokens FIRST, while the wallet still has SOL to pay rent and fees. SOL
 * LAST, as a sweep of whatever remains. Sweeping SOL first would leave every
 * token stranded in an account whose owner can no longer afford to move them,
 * and the recovery would need the compromised key again.
 *
 * WHY IT IS HERE AND NOT AT THE REPOSITORY ROOT
 *
 * CLAUDE.md rule 10 puts entry points at the root, and this is an entry point.
 * It sits here anyway because it imports @solana/web3.js and @solana/spl-token,
 * and the only node_modules in this tree holding them is this directory's. A
 * copy at the root would need NODE_PATH set or a second package.json, which is
 * a worse trade than one documented exception -- rule 10 is explicit that it is
 * the direction for new code, not a licence for churn that buys nothing.
 *
 * USAGE
 *
 *   cd <repo>/swap_terminal/grc-sol-swap/abstergo_exchange
 *   node rotate_solana_key.mjs --network devnet                 # report only
 *   node rotate_solana_key.mjs --network devnet --move          # do it
 *
 * Run it from that directory: it uses the @solana/web3.js and @solana/spl-token
 * already in its node_modules, so nothing new is installed.
 */

import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import {
  Connection, Keypair, PublicKey, SystemProgram, Transaction,
  sendAndConfirmTransaction, LAMPORTS_PER_SOL,
} from '@solana/web3.js';
import {
  TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID, getAssociatedTokenAddress,
  createAssociatedTokenAccountInstruction, createTransferCheckedInstruction,
} from '@solana/spl-token';

const argv = process.argv.slice(2);
const flag = (name) => argv.includes(name);
const value = (name, fallback) => {
  const i = argv.indexOf(name);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : fallback;
};

const NETWORK = value('--network', 'devnet');
const OLD_KEY_PATH = value('--old-key', 'wgrc.json');
const MOVE = flag('--move');
const ENDPOINTS = {
  devnet: 'https://api.devnet.solana.com',
  testnet: 'https://api.testnet.solana.com',
  'mainnet-beta': 'https://api.mainnet-beta.solana.com',
};

// A fee-per-signature floor with headroom. Solana's base fee is 5000 lamports
// per signature; this leaves room for a couple of signatures plus any priority
// fee the cluster is charging, so the sweep cannot fail for being one lamport
// short. Anything left behind is dust on a key that is being abandoned anyway.
const SWEEP_RESERVE_LAMPORTS = 25_000;

function die(message) {
  console.error(`\nREFUSED: ${message}\n`);
  process.exit(2);
}

function loadKeypair(file) {
  if (!fs.existsSync(file)) die(`${file} does not exist. Nothing was read and nothing was done.`);
  const parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
  if (!Array.isArray(parsed) || parsed.length !== 64) {
    die(`${file} is not a 64-byte keypair array. Nothing was done.`);
  }
  return Keypair.fromSecretKey(Uint8Array.from(parsed));
}

/** Refuse to write the replacement anywhere git could ever see it. */
function newKeyDestination() {
  const dir = path.join(os.homedir(), '.config', 'solana');
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const file = path.join(dir, `swap-terminal-${stamp}.json`);
  let probe = dir;
  while (probe !== path.dirname(probe)) {
    if (fs.existsSync(path.join(probe, '.git'))) {
      die(`${dir} is inside a git repository (${probe}). The replacement key must not be.`);
    }
    probe = path.dirname(probe);
  }
  return { dir, file };
}

const sol = (lamports) => `${(lamports / LAMPORTS_PER_SOL).toFixed(9)} SOL (${lamports} lamports)`;

async function main() {
  const endpoint = ENDPOINTS[NETWORK];
  if (!endpoint) die(`unknown --network ${NETWORK}. Use devnet, testnet or mainnet-beta.`);
  if (NETWORK === 'mainnet-beta' && !flag('--i-understand-this-is-mainnet')) {
    die('mainnet-beta needs --i-understand-this-is-mainnet as well. This moves real money.');
  }

  const old = loadKeypair(OLD_KEY_PATH);
  const conn = new Connection(endpoint, 'confirmed');

  console.log('solana key rotation -- report, and transfers behind --move');
  console.log(`  mode        ${MOVE ? 'MOVE -- this will send transactions' : 'REPORT ONLY -- nothing will be sent'}`);
  console.log(`  network     ${NETWORK}  (${endpoint})`);
  console.log(`  compromised ${old.publicKey.toBase58()}`);
  console.log(`  source file ${path.resolve(OLD_KEY_PATH)}  <- treat as PUBLIC; it was pushed to git`);
  console.log('');

  const lamports = await conn.getBalance(old.publicKey);
  console.log(`SOL balance   ${sol(lamports)}`);

  // Both token programs: a mint created under Token-2022 is invisible to a
  // query that only asks the original program, and "no tokens" would be the
  // wrong answer rather than a missing one.
  const holdings = [];
  for (const programId of [TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID]) {
    const { value: accounts } = await conn.getParsedTokenAccountsByOwner(old.publicKey, { programId });
    for (const { pubkey, account } of accounts) {
      const info = account.data.parsed.info;
      const amount = info.tokenAmount;
      if (amount.amount === '0') continue;
      holdings.push({
        tokenAccount: pubkey,
        mint: new PublicKey(info.mint),
        programId,
        raw: BigInt(amount.amount),
        decimals: amount.decimals,
        display: amount.uiAmountString,
      });
    }
  }

  if (holdings.length === 0) {
    console.log('token accounts  (none)  <- no SPL token balance on this network');
  } else {
    console.log(`token accounts  ${holdings.length} with a non-zero balance:`);
    for (const h of holdings) {
      console.log(`    ${h.display} of mint ${h.mint.toBase58()}  (decimals ${h.decimals}, account ${h.tokenAccount.toBase58()})`);
    }
  }
  console.log('');

  if (lamports === 0 && holdings.length === 0) {
    console.log('NOTHING TO MOVE on this network. If you expected a balance, check --network:');
    console.log('  devnet and testnet are different chains and a faucet drop lands on exactly one.');
    return;
  }

  if (!MOVE) {
    console.log('REPORT ONLY -- nothing was sent and no new key was written.');
    console.log('Re-run with --move to rotate. Order: tokens first (while this wallet can still');
    console.log('pay their rent and fees), then a sweep of the remaining SOL.');
    return;
  }

  const { dir, file } = newKeyDestination();
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  if (fs.existsSync(file)) die(`${file} already exists; refusing to overwrite a key.`);
  const fresh = Keypair.generate();
  fs.writeFileSync(file, JSON.stringify(Array.from(fresh.secretKey)), { mode: 0o600 });
  console.log(`new key     ${fresh.publicKey.toBase58()}`);
  console.log(`            written to ${file} (mode 0600, outside any git repository)`);
  console.log('');

  // Tokens first. Each needs the destination ATA to exist, and creating it is
  // paid for in SOL by the OLD wallet -- which is why the SOL sweep is last.
  for (const h of holdings) {
    const fromAta = h.tokenAccount;
    const toAta = await getAssociatedTokenAddress(h.mint, fresh.publicKey, false, h.programId);
    const tx = new Transaction();
    if ((await conn.getAccountInfo(toAta)) === null) {
      tx.add(createAssociatedTokenAccountInstruction(old.publicKey, toAta, fresh.publicKey, h.mint, h.programId));
    }
    tx.add(createTransferCheckedInstruction(fromAta, h.mint, toAta, old.publicKey, h.raw, h.decimals, [], h.programId));
    const sig = await sendAndConfirmTransaction(conn, tx, [old], { commitment: 'confirmed' });
    console.log(`moved       ${h.display} of ${h.mint.toBase58()}`);
    console.log(`            signature ${sig}`);
  }

  // SOL last: whatever survives the token transfers, minus a fee reserve.
  const remaining = await conn.getBalance(old.publicKey);
  const sweep = remaining - SWEEP_RESERVE_LAMPORTS;
  if (sweep <= 0) {
    console.log(`SOL sweep   skipped: ${sol(remaining)} left, at or under the ${SWEEP_RESERVE_LAMPORTS}-lamport fee reserve`);
  } else {
    const tx = new Transaction().add(SystemProgram.transfer({
      fromPubkey: old.publicKey, toPubkey: fresh.publicKey, lamports: sweep,
    }));
    const sig = await sendAndConfirmTransaction(conn, tx, [old], { commitment: 'confirmed' });
    console.log(`swept       ${sol(sweep)}`);
    console.log(`            signature ${sig}`);
  }

  // Read the result back off the chain. A signature says a transaction was
  // accepted, not that the balance is where you wanted it.
  console.log('');
  console.log('VERIFIED BY READING THE CHAIN BACK:');
  console.log(`  old ${old.publicKey.toBase58()}  ${sol(await conn.getBalance(old.publicKey))}`);
  console.log(`  new ${fresh.publicKey.toBase58()}  ${sol(await conn.getBalance(fresh.publicKey))}`);
  for (const programId of [TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID]) {
    const { value: accounts } = await conn.getParsedTokenAccountsByOwner(fresh.publicKey, { programId });
    for (const { account } of accounts) {
      const info = account.data.parsed.info;
      if (info.tokenAmount.amount !== '0') {
        console.log(`  new holds ${info.tokenAmount.uiAmountString} of ${info.mint}`);
      }
    }
  }
  console.log('');
  console.log(`NEXT: point your configuration at ${file} and at the new public key.`);
  console.log('The old key stays compromised forever. Do not reuse it for anything.');
}

main().catch((error) => {
  console.error(`\nFAILED: ${error.message}`);
  console.error('Nothing further was attempted. Re-run the report (without --move) to see current state.');
  process.exit(1);
});
