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
