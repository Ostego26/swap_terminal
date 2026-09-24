/**
 * Can two concurrent claims both authorize one payout? Measured, not reasoned about.
 *
 * Role: test / measurement (seeds a real store file, runs the real functions)
 * Reads: ../intent_store.js
 * Writes: a throwaway swap_intents.json under the OS temp directory, created
 *        and removed per test. SWAP_INTENTS_PATH and the real store are never
 *        touched -- every test takes the path as an argument.
 * Can move funds: no. No socket is opened, no keypair is read, nothing is
 *        signed and nothing is broadcast. The "payout" in these tests is a
 *        push onto an array.
 * Mainnet-safe: yes
 *
 * HOW TO RUN. Nothing in this repository ran a JavaScript test before
 * 2026-09-24, so this says it explicitly:
 *
 *     cd swap_terminal/grc-sol-swap/abstergo_exchange && npm test
 *
 * or `node --test 'tests/*.test.js'` from the same directory. No `npm
 * install`, no network, no Solana keypair: every import is a Node builtin or a
 * file in this directory. Run on Node v22.22.2.
 *
 * WHAT THIS FILE IS FOR.
 *
 * The old /execute handler read the store, compared `status === 'verified'`,
 * wrote 'paying', and then awaited a Solana transfer. That is check-then-act
 * across an await, and it is the same defect that
 * tests/test_payout_concurrency.py measures on the Python side of this
 * repository with two payout workers and one swap. Rule 17 says not to leave
 * it as reasoning: the first test below reproduces the OLD shape against a
 * real file and counts the payouts it authorizes, and the second runs the REAL
 * claimIntentForPayout under the same interleaving and counts again.
 *
 * MEASURED 2026-09-24 on this tree -- the numbers printed by the run:
 *
 *     old read-then-decide shape, 2 concurrent callers   2 payouts authorized
 *     claimIntentForPayout,        2 concurrent callers   1 payout authorized
 *     claimIntentForPayout,       20 concurrent callers   1 payout authorized
 *
 * The first of those is a characterization of a defect that no longer exists
 * in server.js; it is kept because a fix whose failure case was never observed
 * is a fix nobody can check. It reimplements the old sequence deliberately --
 * which the "verify by behavior" principle otherwise forbids -- and says so
 * here so the next reader does not mistake it for a test of live code. The
 * tests of live code are all the others.
 */

import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import {
  CLAIMABLE_STATUSES,
  claimIntentForPayout,
  createIntent,
  formatDurationMs,
  getIntentById,
  readStore,
  recordPayoutFailure,
  recordPayoutSuccess,
  updateIntent,
} from '../intent_store.js';

/** One hour ahead, so a seeded intent is live rather than expired. */
function futureIso(msAhead = 3600_000) {
  return new Date(Date.now() + msAhead).toISOString();
}

function seedStore(intents) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'swap_intents_test_'));
  const storePath = path.join(dir, 'swap_intents.json');
  fs.writeFileSync(storePath, JSON.stringify({ intents }, null, 2));
  return storePath;
}

function verifiedIntent(intentId = 'si_test', overrides = {}) {
  return {
    intentId,
    status: 'verified',
    createdAt: new Date().toISOString(),
    expiresAt: futureIso(),
    gridcoinDepositAddress: 'GRC_DEPOSIT_ADDRESS_PLACEHOLDER',
    destinationSolanaAddress: 'SoLDestinationPlaceholder11111111111111111',
    expectedGrcAmount: 100,
    expectedQuote: { lamports: 5560821, solAmount: 0.005560821 },
    verifiedDeposit: { gridcoinTxid: 'seeded-txid', confirmations: 6, receivedGrcAmount: 100 },
    payout: null,
    ...overrides,
  };
}

test('THE OLD SHAPE: read, decide, write, await -- two callers, two payouts', async () => {
  // A deliberate reimplementation of the sequence server.js used before
  // 2026-09-24, so the defect can be observed rather than described. It is the
  // only test in this file that does not call live code, and it exists to give
  // the next test something to be measured against.
  const storePath = seedStore([verifiedIntent()]);
  const payouts = [];

  async function oldExecute() {
    const intent = getIntentById(storePath, 'si_test');       // read
    if (intent.status !== 'verified') return;                 // decide
    await new Promise((resolve) => setImmediate(resolve));    // any await at all
    await updateIntent(storePath, 'si_test', (current) => ({ ...current, status: 'paying' }));
    payouts.push(intent.expectedQuote.lamports);              // "send"
  }

  await Promise.all([oldExecute(), oldExecute()]);

  assert.equal(payouts.length, 2, 'the old shape was expected to authorize two payouts for one deposit');
  console.log(`  old read-then-decide shape, 2 concurrent callers -> ${payouts.length} payouts authorized  <- the defect`);
});

test('THE FIX: two concurrent claims, exactly one wins', async () => {
  const storePath = seedStore([verifiedIntent()]);
  const payouts = [];

  async function execute() {
    const claim = await claimIntentForPayout(storePath, 'si_test');
    if (!claim.claimed) return claim;
    await new Promise((resolve) => setImmediate(resolve)); // stand-in for sendSolPayout
    payouts.push(claim.intent.expectedQuote.lamports);
    await recordPayoutSuccess(storePath, 'si_test', claim.claimToken, { signature: 'sig_test' });
    return claim;
  }

  const [a, b] = await Promise.all([execute(), execute()]);

  assert.equal(payouts.length, 1, 'exactly one payout must be authorized for one intent');
  assert.equal([a.claimed, b.claimed].filter(Boolean).length, 1);

  const loser = a.claimed ? b : a;
  assert.equal(loser.reason, 'not_claimable');
  // The loser is told WHICH status refused it -- 'paying' if it lost the race,
  // 'paid' if the winner had already finished. Both are correct; what matters
  // is that it is not 'verified'.
  assert.ok(['paying', 'paid'].includes(loser.status), `loser saw status ${loser.status}`);

  assert.equal(getIntentById(storePath, 'si_test').status, 'paid');
  console.log(`  claimIntentForPayout, 2 concurrent callers -> ${payouts.length} payout authorized`);
});

test('THE FIX AT SCALE: twenty concurrent claims, still exactly one', async () => {
  // Two callers can pass by luck of scheduling. Twenty cannot.
  const storePath = seedStore([verifiedIntent()]);
  const claims = await Promise.all(
    Array.from({ length: 20 }, () => claimIntentForPayout(storePath, 'si_test')),
  );
  const won = claims.filter((claim) => claim.claimed);
  assert.equal(won.length, 1);
  // Every claim token is distinct when granted, so the store's record names
  // exactly one holder.
  assert.equal(readStore(storePath).intents[0].payout.claimToken, won[0].claimToken);
  console.log(`  claimIntentForPayout, 20 concurrent callers -> ${won.length} payout authorized`);
});

test('ACROSS A RESTART: a durable `paying` is not claimable by a fresh reader', async () => {
  // This is the restart guarantee, and it is testable without restarting a
  // process because what makes it true is that the claim is on DISK before
  // claimIntentForPayout returns. Reading the file back is exactly what a
  // restarted process would do.
  const storePath = seedStore([verifiedIntent()]);
  const first = await claimIntentForPayout(storePath, 'si_test');
  assert.equal(first.claimed, true);

  const onDisk = JSON.parse(fs.readFileSync(storePath, 'utf8'));
  assert.equal(onDisk.intents[0].status, 'paying', 'the claim must be durable before the caller may send');

  const second = await claimIntentForPayout(storePath, 'si_test');
  assert.equal(second.claimed, false);
  assert.equal(second.status, 'paying');
});

test('A PAID INTENT IS NEVER PAYABLE AGAIN', async () => {
  const storePath = seedStore([verifiedIntent('si_test', { status: 'paid', payout: { signature: 'already' } })]);
  const claim = await claimIntentForPayout(storePath, 'si_test');
  assert.equal(claim.claimed, false);
  assert.equal(claim.status, 'paid');
});

test('A FAILED PAYOUT IS TERMINAL -- it does not roll back to verified', async () => {
  // The old error handler set 'paying' back to 'verified' on ANY error out of
  // sendSolPayout, including a confirmation timeout on a transfer that may
  // already have landed. That re-armed a possibly-completed payout.
  const storePath = seedStore([verifiedIntent()]);
  const claim = await claimIntentForPayout(storePath, 'si_test');
  await recordPayoutFailure(storePath, 'si_test', claim.claimToken, 'confirmation timed out');

  const after = getIntentById(storePath, 'si_test');
  assert.equal(after.status, 'payout_failed');
  assert.notEqual(after.status, 'verified');
  assert.equal(after.payout.error, 'confirmation timed out');

  const retry = await claimIntentForPayout(storePath, 'si_test');
  assert.equal(retry.claimed, false, 'a failed payout must need an operator, not an automatic retry');
  assert.equal(retry.reason, 'not_claimable');
});

test('a stale claim token cannot overwrite a newer payout record', async () => {
  const storePath = seedStore([verifiedIntent()]);
  const claim = await claimIntentForPayout(storePath, 'si_test');
  await recordPayoutSuccess(storePath, 'si_test', claim.claimToken, { signature: 'real_signature' });

  await assert.rejects(
    () => recordPayoutSuccess(storePath, 'si_test', 'claim_somethingelse', { signature: 'wrong_signature' }),
    /no longer the live claim/,
  );
  assert.equal(getIntentById(storePath, 'si_test').payout.signature, 'real_signature');
});

test('an expired intent is refused and the expiry is recorded', async () => {
  const storePath = seedStore([verifiedIntent('si_test', { expiresAt: new Date(Date.now() - 1000).toISOString() })]);
  const claim = await claimIntentForPayout(storePath, 'si_test');
  assert.equal(claim.claimed, false);
  assert.equal(claim.reason, 'expired');
  assert.equal(getIntentById(storePath, 'si_test').status, 'expired');
});

test('verified with no deposit is reported as inconsistent, not paid', async () => {
  const storePath = seedStore([verifiedIntent('si_test', { verifiedDeposit: null })]);
  const claim = await claimIntentForPayout(storePath, 'si_test');
  assert.equal(claim.claimed, false);
  assert.equal(claim.reason, 'verified_without_deposit');
});

test('only "verified" is claimable, and the set says so', async () => {
  assert.deepEqual([...CLAIMABLE_STATUSES], ['verified']);
  for (const status of ['awaiting_deposit', 'paying', 'paid', 'payout_failed', 'expired', 'weird']) {
    const storePath = seedStore([verifiedIntent('si_test', { status })]);
    const claim = await claimIntentForPayout(storePath, 'si_test');
    assert.equal(claim.claimed, false, `status '${status}' was claimable and must not be`);
  }
});

test('A CORRUPT STORE THROWS -- it does not read as "no intents"', () => {
  // The old loadIntentStore had `catch { return { intents: [] } }`, so a
  // truncated file made every intent vanish and the next created intent
  // overwrote the record of a verified, unpaid deposit.
  const storePath = seedStore([verifiedIntent()]);
  fs.writeFileSync(storePath, '{"intents": [{"intentId": "si_te');

  const error = (() => {
    try {
      readStore(storePath);
    } catch (thrown) {
      return thrown;
    }
    throw new assert.AssertionError({ message: 'a truncated store must not parse' });
  })();

  assert.equal(error.statusCode, 500);
  assert.match(error.message, /not valid JSON/);
  assert.match(error.message, /Refusing to treat this as "no intents"/);
});

test('a store whose top level is not {intents: []} also throws', () => {
  const storePath = seedStore([]);
  for (const bad of ['null', '[]', '{"intents": "nope"}', '"a string"']) {
    fs.writeFileSync(storePath, bad);
    assert.throws(() => readStore(storePath), /Refusing to treat this as "no intents"|no "intents" array/);
  }
});

test('WRITES ARE ATOMIC: no reader ever sees a partial document', async () => {
  // The guarantee comes from rename(2), so the test is: interleave many writes
  // with many reads and assert every read parsed. A truncate-in-place writer
  // fails this; a temp-file-plus-rename writer cannot.
  const storePath = seedStore([verifiedIntent('si_0', { status: 'awaiting_deposit' })]);
  let reads = 0;

  const writer = (async () => {
    for (let i = 1; i <= 50; i += 1) {
      await createIntent(storePath, verifiedIntent(`si_${i}`, { status: 'awaiting_deposit' }));
    }
  })();

  const reader = (async () => {
    while (reads < 400) {
      // Throws on a partial document, which is the assertion.
      readStore(storePath);
      reads += 1;
      await new Promise((resolve) => setImmediate(resolve));
    }
  })();

  await Promise.all([writer, reader]);
  assert.equal(readStore(storePath).intents.length, 51, 'every concurrent create must survive');
  console.log(`  ${reads} interleaved reads during 50 writes, 0 partial documents observed`);
});

test('no temp or lock file is left behind after a successful write', async () => {
  const storePath = seedStore([verifiedIntent()]);
  await claimIntentForPayout(storePath, 'si_test');
  const leftovers = fs.readdirSync(path.dirname(storePath)).filter((name) => name !== 'swap_intents.json');
  assert.deepEqual(leftovers, [], `stray files left behind: ${leftovers.join(', ')}`);
});

test('the lock is released even when the critical section throws', async () => {
  const storePath = seedStore([verifiedIntent()]);
  await assert.rejects(() => updateIntent(storePath, 'si_missing', (x) => x));
  // If the finally block did not run, this would now time out after 5s.
  const claim = await claimIntentForPayout(storePath, 'si_test');
  assert.equal(claim.claimed, true);
});

test('an async updater is refused rather than silently reopening the race', async () => {
  const storePath = seedStore([verifiedIntent()]);
  await assert.rejects(
    () => updateIntent(storePath, 'si_test', async (current) => current),
    /must be synchronous/,
  );
});

test('a held cross-process lock makes a claim fail closed, not pay', async () => {
  // Simulates a second process holding the lock: the directory exists and is
  // never released. The claim must refuse within the timeout and must NOT
  // authorize a payout. Refusing is the safe failure -- the alternative
  // (breaking the lock on a timeout) is guessing the holder is dead.
  const storePath = seedStore([verifiedIntent()]);
  fs.mkdirSync(`${storePath}.lock`);
  try {
    const startedAt = Date.now();
    const error = await claimIntentForPayout(storePath, 'si_test').then(
      () => null,
      (thrown) => thrown,
    );
    assert.ok(error, 'a held lock must refuse the claim');
    assert.equal(error.statusCode, 503);
    assert.match(error.message, /never broken automatically/);
    assert.equal(getIntentById(storePath, 'si_test').status, 'verified', 'nothing may have been claimed');
    console.log(`  held lock refused a claim after ${formatDurationMs(Date.now() - startedAt)}`);
  } finally {
    fs.rmSync(`${storePath}.lock`, { recursive: true, force: true });
  }
});

test('formatDurationMs uses the micro sign and no space, per rule 6', () => {
  // `2.3µfn`, never `2.3 µfn` and never `ufn`. An ASCII "u" in displayed
  // output is a defect, the same as printing a wrong number would be.
  const rendered = formatDurationMs(1209.6);
  assert.equal(rendered, '1.0µfn (1.2s)');
  assert.ok(!rendered.includes('ufn'), 'the unit must be the micro sign, not an ASCII u');
  assert.ok(!rendered.includes(' µfn'), 'no space before the unit');
});
