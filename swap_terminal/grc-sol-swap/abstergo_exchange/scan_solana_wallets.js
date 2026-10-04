import fs from 'fs';
import path from 'path';
import { Keypair } from '@solana/web3.js';

const target = process.argv[2] || '';
const roots = process.argv.slice(3).length ? process.argv.slice(3) : [process.cwd(), process.env.HOME || '.'];
const seen = new Set();

function looksLikeSecretArray(value) {
  return Array.isArray(value) && (value.length === 64 || value.length === 32) && value.every((n) => Number.isInteger(n));
}

function tryFile(filePath) {
  if (seen.has(filePath)) return;
  seen.add(filePath);
  try {
    const stat = fs.statSync(filePath);
    if (!stat.isFile()) return;
    if (!filePath.endsWith('.json')) return;
    const raw = fs.readFileSync(filePath, 'utf8').trim();
    if (!raw.startsWith('[')) return;
    const parsed = JSON.parse(raw);
    if (!looksLikeSecretArray(parsed)) return;
    const kp = Keypair.fromSecretKey(new Uint8Array(parsed));
    const pubkey = kp.publicKey.toBase58();
    const marker = target && pubkey === target ? ' <== MATCH' : '';
    console.log(`${filePath} -> ${pubkey}${marker}`);
  } catch {
    // DELIBERATELY EMPTY, and the reason is this function's whole job: walk() points
    // it at every file under a home directory, so the overwhelming majority of what it
    // opens is not a Solana keypair. An unreadable file, a non-JSON file and a JSON file
    // that is not a 64-byte secret array are all 'not a keypair', which is the same
    // answer as the two early `return`s above and not an error worth a line of output.
    // Reporting them would bury the MATCH line this scan exists to print (rule 14: the
    // signal is the point). Nothing here can mask a failure that matters, because the
    // only thing this function does on success is console.log -- it moves nothing and
    // returns nothing a caller branches on.
  }
}

function walk(dir) {
  let entries;
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return;
  }
  for (const entry of entries) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (['node_modules', '.git', '.venv', 'venv', '__pycache__'].includes(entry.name)) continue;
      walk(full);
    } else {
      tryFile(full);
    }
  }
}

for (const root of roots) {
  walk(path.resolve(root));
}
