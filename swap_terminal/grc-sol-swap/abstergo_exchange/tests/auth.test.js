/**
 * Does the shared-secret check actually refuse the requests it claims to?
 *
 * Role: test / measurement (calls the real functions with seeded inputs)
 * Reads: ../auth.js
 * Writes: nothing
 * Can move funds: no -- no socket is opened, no keypair is read, nothing is
 *        signed, and no Express server is started
 * Mainnet-safe: yes
 *
 * HOW TO RUN. Nothing in this repository ran a JavaScript test before
 * 2026-09-24, so this says it explicitly:
 *
 *     cd swap_terminal/grc-sol-swap/abstergo_exchange && node --test tests/
 *
 * or `npm test` from the same directory. It needs NO `npm install`, NO
 * network, and NO Solana keypair: every import is a Node builtin or a file in
 * this directory. That is a deliberate constraint, not a coincidence -- a test
 * that needs the dependency tree installed is a test that does not get run on
 * the machine where the question comes up. `node --test` ships with Node 18+;
 * this was run on v22.22.2.
 *
 * WHAT IS NOT TESTED HERE, AND CANNOT BE FROM THIS SIDE: that server.js's
 * route handlers call these functions. That is verified by reading the routes,
 * which the "verify by behavior" principle says is not verification. Proving
 * it needs the server running against a real port with the dependency tree
 * installed, and this pass did not do that. Said plainly rather than implied.
 */

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  MIN_RECOMMENDED_SECRET_LENGTH,
  describeSharedSecretConfig,
  hasValidSharedSecret,
  requireSharedSecret,
  sharedSecretMatches,
} from '../auth.js';

/**
 * Call `fn` and return the error it threw.
 *
 * node:assert's `throws()` returns undefined rather than the error (unlike
 * some other assertion libraries), and these tests assert on `statusCode` --
 * the difference between "wrong secret" (401) and "server has no secret"
 * (500), which is the distinction the fix introduced and therefore the thing
 * most worth pinning.
 */
function caught(fn) {
  try {
    fn();
  } catch (error) {
    return error;
  }
  throw new assert.AssertionError({ message: 'expected a throw, got none' });
}

/** A stand-in for an Express request that carries exactly one header. */
function requestWithHeader(value) {
  return {
    get(name) {
      if (name !== 'x-gridcoin-verify-secret') return undefined;
      return value;
    },
  };
}

const SECRET = 'correct-horse-battery-staple-correct-horse';

test('the correct secret matches', () => {
  assert.equal(sharedSecretMatches(SECRET, SECRET), true);
});

test('a wrong secret of the same length does not match', () => {
  const wrong = `${SECRET.slice(0, -1)}X`;
  assert.equal(wrong.length, SECRET.length);
  assert.equal(sharedSecretMatches(wrong, SECRET), false);
});

test('a correct PREFIX does not match -- the attack the old !== enabled', () => {
  // The timing attack recovers the secret one byte at a time by measuring how
  // long a near-miss takes. This asserts the functional half (a prefix is not
  // accepted); the timing half is asserted structurally by the fact that the
  // comparison is over two fixed-length digests, which is not something an
  // assertion can observe.
  for (let i = 1; i < SECRET.length; i += 1) {
    assert.equal(sharedSecretMatches(SECRET.slice(0, i), SECRET), false, `prefix of length ${i} was accepted`);
  }
});

test('every non-string shape is refused rather than throwing', () => {
  // The old comparison would have treated `undefined !== secret` as a refusal
  // too, but a later refactor to `provided.length` or `provided.trim()` would
  // have thrown a TypeError and been reported as a 400 -- a bad request --
  // instead of a 401. Pinning the shapes keeps that from coming back.
  for (const shape of [undefined, null, 0, 1, true, false, {}, [], ['x'], () => SECRET]) {
    assert.equal(sharedSecretMatches(shape, SECRET), false, `${String(shape)} was accepted`);
  }
});

test('an empty or missing CONFIGURED secret never matches, including against itself', () => {
  // Fail closed. Without this, an unset GRIDCOIN_VERIFY_SHARED_SECRET plus a
  // caller sending an empty header would authenticate.
  assert.equal(sharedSecretMatches('', ''), false);
  assert.equal(sharedSecretMatches('', null), false);
  assert.equal(sharedSecretMatches('anything', ''), false);
  assert.equal(sharedSecretMatches(undefined, undefined), false);
});

test('requireSharedSecret throws 401 on a bad secret and returns on a good one', () => {
  const config = { enforcementEnabled: true, secret: SECRET };

  assert.doesNotThrow(() => requireSharedSecret(requestWithHeader(SECRET), config));

  const err = caught(() => requireSharedSecret(requestWithHeader('nope'), config));
  assert.equal(err.statusCode, 401);

  const missing = caught(() => requireSharedSecret(requestWithHeader(undefined), config));
  assert.equal(missing.statusCode, 401);
  // The message must not distinguish "absent" from "wrong": the body is a side
  // channel too.
  assert.equal(missing.message, err.message);
});

test('enforcement enabled with no configured secret is a 500, not a 401', () => {
  // The distinction is the whole point. A 401 tells the caller to try a
  // different secret; there is no secret that would work, because the server
  // has none. The old code threw a plain Error that server.js reported as 400.
  const err = caught(() =>
    requireSharedSecret(requestWithHeader('anything'), { enforcementEnabled: true, secret: null }),
  );
  assert.equal(err.statusCode, 500);
});

test('enforcement disabled lets everything through -- which is why the startup check exists', () => {
  const config = { enforcementEnabled: false, secret: SECRET };
  assert.doesNotThrow(() => requireSharedSecret(requestWithHeader(undefined), config));
  assert.equal(hasValidSharedSecret(requestWithHeader(undefined), config), true);
});

test('startup REFUSES enforcement-off while a payer keypair is loaded', () => {
  // This is the combination that exposed POST /swap-intents/:id/execute -- a
  // signed, final Solana transfer -- to anyone who could open a socket.
  const status = describeSharedSecretConfig({ enforcementEnabled: false, secret: null, payoutEnabled: true });
  assert.ok(status.fatal, 'a disabled check with payouts armed must be fatal, not a warning');
  assert.match(status.fatal, /Refusing to start/);
});

test('the refusal names the CALLER\'s armed route, not a hard-coded one', () => {
  // Measured by running services/gridcoin.js with enforcement off: it refused
  // to start with a message naming POST /swap-intents/:intentId/execute, a
  // route it does not serve. A message that names the wrong thing is a bug of
  // the same seriousness as wrong code (rule 16), and the operator reading it
  // at 3am is the person it misleads.
  const bridge = describeSharedSecretConfig({
    enforcementEnabled: false,
    secret: null,
    payoutEnabled: true,
    armedDescription: 'POST /swap-intents/:intentId/execute -- a Solana transfer',
  });
  assert.match(bridge.fatal, /swap-intents/);

  const deposit = describeSharedSecretConfig({
    enforcementEnabled: false,
    secret: null,
    payoutEnabled: true,
    armedDescription: 'POST /deposit -- a Gridcoin sendtoaddress',
  });
  assert.match(deposit.fatal, /sendtoaddress/);
  assert.ok(!deposit.fatal.includes('swap-intents'), 'the deposit server must not be told about a route it does not serve');

  // And with no description, it still refuses -- it just cannot be specific.
  const unnamed = describeSharedSecretConfig({ enforcementEnabled: false, secret: null, payoutEnabled: true });
  assert.ok(unnamed.fatal);
  assert.match(unnamed.fatal, /an endpoint that moves funds/);
});

test('startup ALLOWS enforcement-off when no payer keypair is loaded, and says so', () => {
  const status = describeSharedSecretConfig({ enforcementEnabled: false, secret: null, payoutEnabled: false });
  assert.equal(status.fatal, null);
  assert.ok(status.lines.some((line) => line.includes('DISABLED')));
  assert.ok(status.lines.some((line) => line.includes('no payer keypair')));
});

test('startup refuses enforcement-on with no secret', () => {
  const status = describeSharedSecretConfig({ enforcementEnabled: true, secret: null, payoutEnabled: false });
  assert.ok(status.fatal);
  assert.match(status.fatal, /GRIDCOIN_VERIFY_SHARED_SECRET is unset/);
});

test('a short secret warns with its length and does not stop the process', () => {
  const short = 'x'.repeat(MIN_RECOMMENDED_SECRET_LENGTH - 1);
  const status = describeSharedSecretConfig({ enforcementEnabled: true, secret: short, payoutEnabled: true });
  assert.equal(status.fatal, null);
  assert.ok(status.lines.some((line) => line.includes(`length ${short.length} chars`)));
  assert.ok(status.lines.some((line) => line.includes('brute-forceable')));
});

test('no startup line ever contains the secret itself', () => {
  // Rule 14 / the chain-safety rules: never print a secret. The LENGTH is not
  // the secret and is what lets a truncated value be spotted.
  const status = describeSharedSecretConfig({ enforcementEnabled: true, secret: SECRET, payoutEnabled: true });
  for (const line of status.lines) {
    assert.ok(!line.includes(SECRET), `a startup line leaked the shared secret: ${line}`);
  }
});
