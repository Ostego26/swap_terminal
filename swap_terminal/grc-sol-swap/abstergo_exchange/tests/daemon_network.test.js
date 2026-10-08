/**
 * The network gate on the one spend path that had none.
 *
 * Role: file (entry point -- `node --test tests/daemon_network.test.js`)
 * Reads: daemon_network.js only
 * Writes: nothing
 * Can move funds: no
 * Mainnet-safe: yes. No socket is opened and no RPC is reachable from here.
 *
 * RUNNABLE WITHOUT node_modules, and that is a requirement rather than a
 * happy accident. `services/gridcoin.js` imports express, axios and
 * @solana/web3.js, so nothing that imports IT can be tested in a container
 * with no package install -- measured 2026-10-08: there is no node_modules in
 * this directory at all. The decision was therefore extracted into a module
 * that imports nothing (rule 10), which is the only reason these assertions
 * exist instead of a paragraph claiming the gate works.
 *
 * WHAT IS AND IS NOT COVERED, said plainly because rule 17 draws this line and
 * rule 16 draws it again: every branch of the DECISION is tested below against
 * seeded RPC answers. The WIRING -- that services/gridcoin.js calls
 * getblockchaininfo on the real daemon and honors the verdict before
 * sendtoaddress -- is NOT tested here and cannot be tested from this container,
 * because it needs a Gridcoin daemon. That half is a proposal until the
 * operator runs it against theirs.
 */

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  GRC_TEST_NETWORKS,
  UNKNOWN_PREFIX,
  isNamed,
  networkFromRpcAnswers,
  spendVerdict,
} from '../daemon_network.js';

test('the allowlist is exactly the Python table, string for string', () => {
  // THIS IS THE RULE 8 PIN. swap_terminal/chains/daemon_network.py:42 holds
  // CHAIN_TEST_NETWORKS["GRC"] = frozenset({"test", "testnet", "regtest"}).
  // Two copies of one allowlist agree on the day they are written and drift
  // from then on, invisibly, because each looks correct in its own file. This
  // assertion is what makes the drift loud: widen or narrow either side and
  // this test names the other file.
  assert.deepEqual(
    [...GRC_TEST_NETWORKS].sort(),
    ['regtest', 'test', 'testnet'],
    'diverged from CHAIN_TEST_NETWORKS["GRC"] in swap_terminal/chains/daemon_network.py:42',
  );
});

test('a modern daemon answers through getblockchaininfo.chain', () => {
  assert.equal(networkFromRpcAnswers({ blockchainInfo: { chain: 'test' } }), 'test');
  assert.equal(networkFromRpcAnswers({ blockchainInfo: { chain: 'main' } }), 'main');
  assert.equal(networkFromRpcAnswers({ blockchainInfo: { chain: 'regtest' } }), 'regtest');
});

test('an older daemon answers through getinfo.testnet, as a NAME not a boolean', () => {
  // The boolean is converted to one vocabulary here rather than at every
  // caller, which is the whole reason chain_network() returns a string.
  assert.equal(networkFromRpcAnswers({ info: { testnet: true } }), 'testnet');
  assert.equal(networkFromRpcAnswers({ info: { testnet: false } }), 'main');
});

test('testnet:false is an ANSWER, not a missing field', () => {
  // The dangerous misreading. `false` is falsy, so a truthiness check here
  // would drop a definitive MAINNET answer into the "unknown" bucket -- and
  // since the gate fails closed, that would at least refuse rather than
  // permit. It is still wrong, because the operator would be told the daemon
  // could not be read when it had in fact said "main" clearly.
  const verdict = spendVerdict(networkFromRpcAnswers({ info: { testnet: false } }));
  assert.equal(verdict.network, 'main');
  assert.equal(verdict.allowed, false);
  assert.match(verdict.reason, /NOT in the GRC test allowlist/);
  assert.doesNotMatch(verdict.reason, /did not name its network/);
});

test('getblockchaininfo raising is not a failure -- it falls through to getinfo', () => {
  // An older build has no getblockchaininfo and answers "Method not found".
  // daemon_network.py:78 treats that as the signal to try the next field,
  // explicitly, and so must this.
  const answers = {
    blockchainInfo: new TypeError('Method not found'),
    info: { testnet: true },
  };
  assert.equal(networkFromRpcAnswers(answers), 'testnet');
  assert.equal(spendVerdict(networkFromRpcAnswers(answers)).allowed, true);
});

test('both routes failing returns unknown, and the reasons come with it', () => {
  const network = networkFromRpcAnswers({
    blockchainInfo: new Error('ECONNREFUSED'),
    info: new Error('ECONNREFUSED'),
  });
  assert.ok(network.startsWith(UNKNOWN_PREFIX), network);
  assert.equal(isNamed(network), false);
  // An operator reads the screen, not the source (rule 14): the string has to
  // say WHY it could not be read, not merely that it could not.
  assert.match(network, /getblockchaininfo/);
  assert.match(network, /getinfo/);
});

test('no route tried at all still refuses, and says that it was not tried', () => {
  const network = networkFromRpcAnswers({});
  assert.equal(isNamed(network), false);
  assert.match(network, /no route answered/);
  assert.equal(spendVerdict(network).allowed, false);
});

test('an unreadable network REFUSES -- fail closed, never "probably testnet"', () => {
  const verdict = spendVerdict(networkFromRpcAnswers({ blockchainInfo: new Error('boom') }));
  assert.equal(verdict.allowed, false);
  assert.match(verdict.reason, /did not name its network/);
  assert.match(verdict.reason, /not evidence of a testnet one/);
});

test('mainnet is refused under every spelling a daemon might use for it', () => {
  // An allowlist, not a denylist. None of these is in the GRC table, and the
  // point of the allowlist is that a network nobody anticipated is refused by
  // construction rather than by someone having thought to add it.
  for (const network of ['main', 'mainnet', 'livenet', 'MAIN', 'Test', 'TESTNET', '']) {
    const verdict = spendVerdict(network);
    assert.equal(verdict.allowed, false, `${network || '(empty string)'} must be refused`);
  }
});

test('every refusal names the condition it refused on', () => {
  // The lesson from 2026-10-07, when a precheck in this repository blocked a
  // correct send and blamed an RPC code the daemon never sent. A refusal that
  // misnames its condition is worse than no refusal, because the operator then
  // debugs the wrong thing.
  for (const network of ['main', UNKNOWN_PREFIX + 'x)', 'regtest']) {
    const verdict = spendVerdict(network);
    assert.equal(typeof verdict.reason, 'string');
    assert.ok(verdict.reason.length > 20, `reason too thin to act on: ${verdict.reason}`);
    assert.equal(verdict.network, network, 'the verdict must echo what it judged');
  }
});

test('the allowlist cannot be widened at runtime', () => {
  // A guard a caller can mutate is a decoration, and THIS ASSERTION ALREADY
  // EARNED ITS KEEP. The first version of daemon_network.js exported
  // Object.freeze(new Set([...])) with a comment claiming exactly what this
  // test checks. It failed on the first run: freeze seals properties, and a
  // Set keeps its members in internal slots, so `.add('main')` succeeded and
  // the gate would have permitted mainnet from then on.
  //
  // Both reaches are checked, because a frozen array fails them differently
  // and a caller might try either.
  assert.throws(
    () => {
      GRC_TEST_NETWORKS.push('main');
    },
    TypeError,
    'push onto the allowlist must throw, not silently succeed',
  );
  assert.equal(
    typeof GRC_TEST_NETWORKS.add,
    'undefined',
    'the exported allowlist must not be a Set -- a frozen Set is mutable through .add',
  );
  assert.equal(spendVerdict('main').allowed, false, 'main must still be refused afterward');
});
