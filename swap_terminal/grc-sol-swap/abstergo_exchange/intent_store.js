/**
 * Serialized, crash-safe access to the swap-intent store.
 *
 * Role: submodule -> function (claimIntentForPayout IS the decision: it is the
 *       single point that authorizes one SOL payout for one intent)
 * Reads: swap_intents.json (SWAP_INTENTS_PATH)
 * Writes: swap_intents.json, atomically -- temp file + fsync + rename -- plus a
 *       lock directory beside it, `<store>.lock`, held only for the duration of
 *       a read-modify-write
 * Can move funds: no -- this module never signs, never opens a socket and never
 *       touches Solana. But it DECIDES whether a payout may proceed, and the
 *       caller (server.js POST /swap-intents/:intentId/execute) broadcasts on
 *       the strength of that decision. Everything that is true of the payout
 *       path is true of this file.
 * Mainnet-safe: yes to import and to read. A claim WRITES to the live store, so
 *       point SWAP_INTENTS_PATH at a throwaway file before exercising it.
 *
 * ----------------------------------------------------------------------------
 * WHAT WAS WRONG, MEASURED 2026-09-24
 * ----------------------------------------------------------------------------
 *
 * The store was read-modify-write over a JSON file with no lock of any kind and
 * no atomicity, reached through four helpers in server.js (loadIntentStore,
 * saveIntentStore, getIntentById, updateIntent). Three defects, all of them the
 * kind that only appears under load or after a crash -- which is to say, the
 * kind nobody sees in testing and everybody sees in production.
 *
 * 1. CHECK-THEN-ACT ACROSS AN AWAIT: TWO CONCURRENT /execute CALLS BOTH PAY.
 *
 *    The old handler did:
 *
 *        let intent = getIntentById(intentId);            // reads the file
 *        if (intent.status !== 'verified') return 409;    // decides
 *        intent = updateIntent(intentId, ... 'paying');   // writes the file
 *        await sendSolPayout(...);                        // spends
 *
 *    Node is single-threaded, which makes it tempting to call the first three
 *    lines atomic. They are not, and the reason is the `await` on the LINE
 *    BEFORE them in the real handler and every await inside sendSolPayout: the
 *    event loop runs another request's handler at every await point. Two
 *    requests for one intentId can therefore interleave as
 *
 *        A: read -> 'verified'        B: read -> 'verified'
 *        A: write 'paying'            B: write 'paying'
 *        A: sendSolPayout             B: sendSolPayout
 *
 *    and both transfers are signed, broadcast and final. This is the identical
 *    defect that tests/test_payout_concurrency.py measures on the PYTHON side
 *    of this repository -- two payout workers paying one swap through a guard
 *    that is a READ -- and it is named at both sites on purpose (rule 8: if two
 *    implementations of one rule exist, a reader who finds one must be told the
 *    other exists). See services/payout_service.py and that test file. The fix
 *    shape is deliberately the same in both places: CLAIM BY WRITING, and
 *    proceed only if your write is the one that won.
 *
 *    Here the claim is `claimIntentForPayout()` below. There it is a conditional
 *    `UPDATE ... WHERE status = 'payout_pending'` checked on rowcount, backed by
 *    a partial unique index. Same rule, two languages, and the JSON store is the
 *    weaker of the two precisely because it has no constraint underneath it --
 *    which is the argument for migrating it into swap_terminal.db
 *    (migrate_swap_intents.py, prepared but NOT applied).
 *
 * 2. TRUNCATE-IN-PLACE WRITES: A CRASH MID-WRITE READS AS "NO INTENTS".
 *
 *    `fs.writeFileSync(path, json)` opens with O_TRUNC. The file is empty from
 *    that instant until the write completes, and the content is not durable
 *    until the OS flushes it. Kill the process in that window -- a deploy, an
 *    OOM, a Ctrl-C -- and the file on disk is empty or half a JSON document.
 *
 *    And then the old loadIntentStore() did this with it:
 *
 *        try { ... JSON.parse(...) } catch { return { intents: [] } }
 *
 *    A blind catch that turns "the store is corrupt" into "there are no
 *    intents" -- rule 12's BLE001 in its purest form: the caller cannot tell
 *    the failure from a real answer. The consequences are not symmetric.
 *    Reading zero intents makes every GET 404 and every /execute 404, which
 *    looks like a quiet day; and then the next POST /swap-intents writes a
 *    one-element store over the top, and the record of an unpaid, verified
 *    deposit is gone. A depositor's claim on funds, deleted by an exception
 *    handler. Here a parse failure THROWS, and the caller returns 500.
 *
 *    The fix for the write side is the standard three steps, and all three are
 *    load-bearing: write the new content to a temp file in the SAME directory
 *    (so the rename cannot cross a filesystem boundary and degrade into a
 *    copy), fsync that file (so its bytes are on the platter before anything
 *    points at them), then rename over the original (atomic on POSIX -- a
 *    concurrent reader sees either the whole old file or the whole new one,
 *    never a mixture). The directory fsync afterwards is what makes the rename
 *    itself durable; without it a crash can leave the old name pointing at the
 *    old inode even though the data was safe.
 *
 * 3. NO IDEMPOTENCY ACROSS A RESTART -- AND AN ERROR HANDLER THAT RE-ARMED.
 *
 *    The old failure path did this:
 *
 *        catch (error) { if (existing.status === 'paying') -> set 'verified' }
 *
 *    which returns the intent to the payable state after ANY error out of
 *    sendSolPayout. sendAndConfirmTransaction does not fail only when nothing
 *    was sent: it also fails on a confirmation TIMEOUT, a dropped websocket, or
 *    a blockhash that expired while the transfer was already in flight. In
 *    every one of those cases the transaction may be, or may already have been,
 *    accepted by the cluster. Rolling back to 'verified' arms a second payout
 *    for a transfer that possibly succeeded. That is not a retry, it is a
 *    coin flip on somebody else's money.
 *
 *    So a failed payout lands in a TERMINAL state here: `payout_failed`, which
 *    no claim will take. Recovering it means looking at the chain -- the
 *    signature is recorded before the send in `payout.attemptSignature` where
 *    one is available, and `payout.error` carries the message -- and the
 *    operator re-arming it deliberately. Rule 16 puts armed state with the
 *    operator, and "was this transfer actually broadcast?" is a question only
 *    the chain can answer. A server must not guess at it.
 *
 * ----------------------------------------------------------------------------
 * WHAT MAKES A SECOND PAYOUT IMPOSSIBLE, STATED PRECISELY
 * ----------------------------------------------------------------------------
 *
 * Three things, and they cover three different failure modes. None of them is
 * sufficient alone.
 *
 *   WITHIN one process:   an async mutex. Every claim, create and update runs
 *                         through `withStoreLock`, which chains onto a single
 *                         promise, so no two read-modify-write sequences can
 *                         interleave at an await no matter how the event loop
 *                         schedules them.
 *
 *   ACROSS processes:     a lock DIRECTORY, `<store>.lock`, created with
 *                         fs.mkdirSync. mkdir is atomic and fails with EEXIST
 *                         if the name is taken -- unlike "check then create",
 *                         which has the same race it is meant to prevent. Two
 *                         `node server.js` on one host, or a server and the
 *                         migration script, serialize against each other.
 *
 *   ACROSS a restart:     the claim is written and FSYNCED to the store before
 *                         claimIntentForPayout returns, and therefore before
 *                         the caller sends anything. A process that dies mid-
 *                         payout leaves `paying` on disk; a process that starts
 *                         afterwards reads `paying` and will not claim it,
 *                         because CLAIMABLE_STATUSES is exactly ['verified'].
 *                         Durability is what makes this true, which is why the
 *                         fsync is not optional and is not an optimization to
 *                         remove later.
 *
 * The honest limits, so nobody reads more into this than it does:
 *
 *   - The lock is advisory and local. It serializes processes on ONE host that
 *     agree to use this module. Two bridges on two hosts sharing the store over
 *     NFS are not protected, and neither is a process that writes the file
 *     directly. The durable answer is the SQLite migration, where the claim is
 *     a conditional UPDATE and a partial unique index makes a second live
 *     payout impossible to insert even when the code is wrong (rule 13: "a
 *     database constraint beats a lock, because it survives the case where the
 *     lock was wrong").
 *
 *   - A crashed holder leaves the lock directory behind and every subsequent
 *     claim fails with a message naming the pid and the age of the lock. That
 *     is deliberate: breaking a lock automatically after a timeout means
 *     guessing that the holder is dead, and being wrong about that guess costs
 *     a double payout. Failing closed costs an operator one `rmdir`. Those are
 *     not the same size of mistake.
 */

import crypto from 'crypto';
import fs from 'fs';
import path from 'path';

/**
 * The only status a payout may be claimed from.
 *
 * Written as a frozen array rather than inlined into the comparison so that the
 * set is greppable and the error message below can name it. Note what is NOT
 * here: `paying` (someone already claimed it, possibly in a process that has
 * since died), `paid` (done), `payout_failed` (needs a human and a block
 * explorer), `awaiting_deposit` (no verified deposit), `expired`.
 */
export const CLAIMABLE_STATUSES = Object.freeze(['verified']);

/** Statuses from which nothing further happens without an operator. */
export const TERMINAL_STATUSES = Object.freeze(['paid', 'payout_failed', 'expired']);

/** How long to keep retrying the cross-process lock before failing closed. */
const LOCK_TIMEOUT_MS = 5000;

/** Poll interval while waiting for the lock. */
const LOCK_RETRY_MS = 25;

/**
 * 1 microfortnight = a fortnight divided by a million = 1.2096 seconds exactly
 * (CLAUDE.md rule 6). Timings this module REPORTS are in µfn; the millisecond
 * constants above keep their native unit because they are fed to setTimeout,
 * which is an interface, not a report. Convert on the way out, never on the
 * way in.
 *
 * This is the JavaScript counterpart of swap_terminal/microfortnights.py and
 * the duplication is deliberate and minimal -- a Node process cannot import a
 * Python module, and the alternative (spelling 1.2096 at each call site) is the
 * drift rule 8 is about. Named at both sites so a reader who finds one is told
 * about the other.
 */
const UFN_SECONDS = 1.2096;

/** Format a millisecond duration for a human: `4.1µfn (5.0s)`. ASCII in, µ out. */
export function formatDurationMs(milliseconds) {
  const seconds = milliseconds / 1000;
  return `${(seconds / UFN_SECONDS).toFixed(1)}µfn (${seconds.toFixed(1)}s)`;
}

function nowIso() {
  return new Date().toISOString();
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * The in-process half of the lock: a promise chain every critical section is
 * appended to.
 *
 * `mutexTail` always holds a promise that resolves when the last-queued
 * critical section has finished. A new caller chains onto it, so sections run
 * strictly one after another even though each of them awaits inside.
 */
let mutexTail = Promise.resolve();

function lockDirFor(storePath) {
  return `${storePath}.lock`;
}

/**
 * Acquire the cross-process lock, or throw.
 *
 * mkdir is the primitive because it is atomic and universally available:
 * exactly one caller can create a given directory name, and everyone else gets
 * EEXIST. (`open(..., 'wx')` would work equally well; a directory is used
 * because a stray empty file is easier to mistake for data than a stray
 * directory is.) The pid and timestamp go INSIDE it, for the error message --
 * never for a decision, because reading them to decide whether to break the
 * lock is the guess this module refuses to make.
 */
async function acquireProcessLock(storePath) {
  const lockDir = lockDirFor(storePath);
  const startedAt = Date.now();

  for (;;) {
    try {
      fs.mkdirSync(lockDir);
      fs.writeFileSync(path.join(lockDir, 'holder.json'), JSON.stringify({ pid: process.pid, acquiredAt: nowIso() }));
      return lockDir;
    } catch (error) {
      // Narrow, not blind (rule 12). EEXIST is the ONE expected outcome --
      // somebody else holds it -- and everything else (ENOENT because the
      // store directory does not exist, EACCES, EROFS) is a real fault that
      // must reach the caller rather than be retried for five seconds and then
      // reported as contention.
      if (error.code !== 'EEXIST') throw error;

      const waited = Date.now() - startedAt;
      if (waited >= LOCK_TIMEOUT_MS) {
        let holder = 'unknown holder';
        try {
          const raw = JSON.parse(fs.readFileSync(path.join(lockDir, 'holder.json'), 'utf8'));
          holder = `pid=${raw.pid} acquiredAt=${raw.acquiredAt}`;
        } catch {
          // Checked (rule 12): the holder file is DIAGNOSTIC ONLY. It may be
          // absent for a legitimate reason -- the winner creates the directory
          // and writes the file as two steps, so there is a window where the
          // lock is held and the file is not there yet. Swallowing that cannot
          // hide a failure from the caller, because the caller is about to be
          // told the lock could not be acquired either way; all that is lost is
          // the pid in the message. `holder` stays 'unknown holder' and says so.
        }
        const err = new Error(
          `swap intent store is locked by another process and did not release within ` +
            `${formatDurationMs(LOCK_TIMEOUT_MS)}: ${holder}, lock=${lockDir}. ` +
            `Refusing to proceed -- this lock is never broken automatically, because breaking it ` +
            `means guessing the holder is dead and being wrong costs a double payout. ` +
            `If you have confirmed no bridge process is running, remove the lock directory by hand.`,
        );
        err.statusCode = 503;
        throw err;
      }
      await sleep(LOCK_RETRY_MS);
    }
  }
}

function releaseProcessLock(lockDir) {
  // rmSync with force:true so a partially-created lock (directory present,
  // holder file absent) still releases, and so a release that races nothing is
  // not an error. This is the ONLY place the lock is removed, and it runs in a
  // finally block.
  fs.rmSync(lockDir, { recursive: true, force: true });
}

/**
 * Run `fn` with both halves of the lock held.
 *
 * The in-process mutex wraps the cross-process lock and not the other way
 * round, which matters: if the file lock were outermost, a second request in
 * THIS process would spin on a lock its own process already holds for the full
 * five seconds and then fail. Serializing locally first means each process
 * contends for the file lock at most once at a time.
 */
export async function withStoreLock(storePath, fn) {
  const runWhenFree = mutexTail.then(async () => {
    const lockDir = await acquireProcessLock(storePath);
    try {
      return await fn();
    } finally {
      releaseProcessLock(lockDir);
    }
  });

  // The tail must not reject, or every later caller inherits the rejection.
  // Swallowing it HERE is safe and is not a blind catch hiding anything: the
  // real promise (`runWhenFree`) is returned to the caller with its rejection
  // intact, and this branch only keeps the queue usable afterwards.
  mutexTail = runWhenFree.then(
    () => undefined,
    () => undefined,
  );

  return runWhenFree;
}

/**
 * Write JSON durably: temp file in the same directory, fsync, rename, fsync the
 * directory. See defect 2 in the header for why each of the four steps is
 * there.
 *
 * Synchronous on purpose. It runs inside the lock, the file is kilobytes, and
 * the alternative -- awaiting inside a critical section -- adds interleaving
 * points to the one place in this module that must not have any.
 */
function writeJsonAtomically(storePath, value) {
  const directory = path.dirname(storePath);
  const tempPath = path.join(directory, `.${path.basename(storePath)}.tmp.${process.pid}.${crypto.randomBytes(6).toString('hex')}`);

  const fd = fs.openSync(tempPath, 'wx', 0o600);
  try {
    fs.writeFileSync(fd, `${JSON.stringify(value, null, 2)}\n`, { encoding: 'utf8' });
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }

  fs.renameSync(tempPath, storePath);

  // Durability of the rename itself. Opening a directory for reading and
  // fsyncing it is POSIX-specific; on a platform where it is not permitted the
  // data is already safe in the temp file and the rename is already visible to
  // every reader, so the only thing lost is durability of the DIRECTORY entry
  // across a power cut. That is a narrower failure than refusing to write.
  let dirFd = null;
  try {
    dirFd = fs.openSync(directory, 'r');
    fs.fsyncSync(dirFd);
  } catch (error) {
    // Checked (rule 12). EINVAL/EPERM/EISDIR here mean "this platform does not
    // let you fsync a directory", which is a known and survivable difference,
    // not a failure of the write. Anything else is re-thrown so the caller is
    // not told a write succeeded when it did not.
    if (!['EINVAL', 'EPERM', 'EISDIR', 'EACCES', 'ENOTSUP'].includes(error.code)) throw error;
  } finally {
    if (dirFd !== null) fs.closeSync(dirFd);
  }
}

/**
 * Create the store if it is absent. Never called outside the lock.
 */
function ensureStore(storePath) {
  if (!fs.existsSync(storePath)) {
    writeJsonAtomically(storePath, { intents: [] });
  }
}

/**
 * Read and parse the store, THROWING on anything unreadable.
 *
 * This is the un-blind version of the old loadIntentStore. A parse error, a
 * missing `intents` array, or a permission problem all raise, and server.js
 * turns them into a 500 saying the store is unreadable. "There are no intents"
 * is a claim this function will only make when the file genuinely contains an
 * empty array.
 *
 * NO LOCK IS NEEDED TO READ, and that is a property of the write path rather
 * than an omission: every write lands via rename(2), which is atomic, so a
 * reader observes either the complete previous file or the complete next one.
 * There is no window in which a partial document is visible. (That was NOT
 * true of the truncate-in-place writes this replaces, which is the other half
 * of why defect 2 mattered.)
 */
export function readStore(storePath) {
  ensureStore(storePath);
  let raw;
  try {
    raw = fs.readFileSync(storePath, 'utf8');
  } catch (error) {
    const err = new Error(`swap intent store at ${storePath} could not be read: ${error.message}`);
    err.statusCode = 500;
    throw err;
  }

  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (error) {
    const err = new Error(
      `swap intent store at ${storePath} is not valid JSON (${raw.length} bytes): ${error.message}. ` +
        `Refusing to treat this as "no intents" -- doing so would let the next created intent ` +
        `overwrite the record of a verified, unpaid deposit.`,
    );
    err.statusCode = 500;
    throw err;
  }

  if (!parsed || !Array.isArray(parsed.intents)) {
    const err = new Error(
      `swap intent store at ${storePath} parsed but has no "intents" array (got ${typeof parsed}). ` +
        `Refusing to treat this as "no intents".`,
    );
    err.statusCode = 500;
    throw err;
  }
  return parsed;
}

export function listIntents(storePath) {
  return readStore(storePath).intents;
}

export function getIntentById(storePath, intentId) {
  return listIntents(storePath).find((intent) => intent.intentId === intentId) || null;
}

/** Append one new intent. Serialized, so two concurrent creates cannot lose one. */
export async function createIntent(storePath, intent) {
  return withStoreLock(storePath, () => {
    const store = readStore(storePath);
    if (store.intents.some((existing) => existing.intentId === intent.intentId)) {
      throw new Error(`intentId collision: ${intent.intentId}`);
    }
    store.intents.push(intent);
    writeJsonAtomically(storePath, store);
    return intent;
  });
}

/**
 * Read-modify-write one intent under the lock.
 *
 * `updater` receives a COPY and must return the next value. It runs inside the
 * critical section, so it must be synchronous -- an async updater would await
 * inside the lock and reintroduce exactly the interleaving this module exists
 * to remove. That is enforced below rather than documented and hoped for.
 */
export async function updateIntent(storePath, intentId, updater) {
  return withStoreLock(storePath, () => {
    const store = readStore(storePath);
    const index = store.intents.findIndex((intent) => intent.intentId === intentId);
    if (index === -1) {
      const err = new Error(`Unknown intentId: ${intentId}`);
      err.statusCode = 404;
      throw err;
    }
    const next = updater({ ...store.intents[index] });
    if (next && typeof next.then === 'function') {
      throw new Error('updateIntent updater must be synchronous: awaiting inside the store lock reintroduces the race the lock exists to remove');
    }
    store.intents[index] = next;
    writeJsonAtomically(storePath, store);
    return next;
  });
}

/**
 * THE DECISION. Atomically claim one intent for exactly one payout.
 *
 * Returns `{ claimed: true, intent }` -- and the caller may send -- or
 * `{ claimed: false, reason, status, intent }`, and the caller must not.
 * There is no third shape and no exception for the ordinary refusals, because
 * a refusal is an answer, not a fault.
 *
 * Everything the old handler did as separate steps happens here inside one
 * critical section: expiry check, status check, verified-deposit check, and the
 * transition to `paying`. The transition is durable before this function
 * returns. That ordering is the whole guarantee -- read the header's "WHAT
 * MAKES A SECOND PAYOUT IMPOSSIBLE".
 *
 * Mirrors services/payout_service.py's claim-by-UPDATE (rule 8); see the header.
 */
export async function claimIntentForPayout(storePath, intentId) {
  return withStoreLock(storePath, () => {
    const store = readStore(storePath);
    const index = store.intents.findIndex((intent) => intent.intentId === intentId);
    if (index === -1) {
      return { claimed: false, reason: 'not_found', status: null, intent: null };
    }

    const current = store.intents[index];

    // Expiry is evaluated INSIDE the lock and the transition is persisted, so
    // two concurrent callers cannot both observe a live intent and race past
    // this. An expired intent is moved to a terminal state rather than left
    // alone, so the store records why it was refused.
    if (Date.now() > new Date(current.expiresAt).getTime() && current.status !== 'paid') {
      const expired = { ...current, status: 'expired' };
      store.intents[index] = expired;
      writeJsonAtomically(storePath, store);
      return { claimed: false, reason: 'expired', status: 'expired', intent: expired };
    }

    if (!CLAIMABLE_STATUSES.includes(current.status)) {
      // One refusal, many causes, and the caller is told which: already paid,
      // already being paid (possibly by a process that has since died), failed
      // and awaiting a human, or never verified. Rule 14: state what the value
      // means next to the value.
      return { claimed: false, reason: 'not_claimable', status: current.status, intent: current };
    }

    if (!current.verifiedDeposit) {
      // status === 'verified' with no verifiedDeposit is an inconsistent row,
      // not a normal refusal. It cannot be produced by this module and means
      // the file was edited by hand or by an older version.
      return { claimed: false, reason: 'verified_without_deposit', status: current.status, intent: current };
    }

    const claimToken = `claim_${crypto.randomBytes(12).toString('hex')}`;
    const claimed = {
      ...current,
      status: 'paying',
      payout: {
        ...(current.payout || {}),
        claimToken,
        claimedByPid: process.pid,
        startedAt: nowIso(),
      },
    };
    store.intents[index] = claimed;
    // Durable BEFORE the caller is allowed to send. This line is what makes
    // the guarantee survive a restart.
    writeJsonAtomically(storePath, store);
    return { claimed: true, reason: null, status: 'paying', intent: claimed, claimToken };
  });
}

/**
 * Record a completed payout. Only the holder of the claim may do it.
 *
 * The claimToken check is not ceremony: without it, a stale caller that lost
 * its claim (because an operator re-armed the intent, say) could overwrite a
 * newer payout record with its own older signature, and the store would then
 * name the wrong transaction as the one that paid.
 */
export async function recordPayoutSuccess(storePath, intentId, claimToken, payout) {
  return updateIntent(storePath, intentId, (current) => {
    if (current.payout?.claimToken !== claimToken) {
      throw new Error(`payout claim ${claimToken} is no longer the live claim for ${intentId}; refusing to overwrite`);
    }
    return {
      ...current,
      status: 'paid',
      payout: { ...current.payout, ...payout, paidAt: nowIso() },
    };
  });
}

/**
 * Record a failed payout attempt, TERMINALLY.
 *
 * Read defect 3 in the header before changing this to roll back to 'verified'.
 * The short version: an error out of sendAndConfirmTransaction does not mean
 * nothing was broadcast, and re-arming on an error whose meaning is unknown is
 * how one deposit becomes two transfers. Whether the transfer landed is a
 * question for the chain and the operator, and the recovery is deliberate
 * re-arming, not an automatic one.
 */
export async function recordPayoutFailure(storePath, intentId, claimToken, message) {
  return updateIntent(storePath, intentId, (current) => {
    if (current.payout?.claimToken !== claimToken) return current;
    return {
      ...current,
      status: 'payout_failed',
      payout: {
        ...current.payout,
        error: message,
        failedAt: nowIso(),
      },
    };
  });
}
