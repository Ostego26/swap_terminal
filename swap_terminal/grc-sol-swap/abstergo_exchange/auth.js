/**
 * Shared-secret authentication for the Solana bridge's Express server.
 *
 * Role: function (the decision -- "is this request authorized?" -- and nothing else)
 * Reads: the `x-gridcoin-verify-secret` request header, and the two environment
 *        values server.js passes in (GRIDCOIN_VERIFY_SHARED_SECRET,
 *        REQUIRE_GRIDCOIN_SHARED_SECRET). It never reads process.env itself:
 *        the caller passes the values, so this module can be tested with
 *        seeded inputs and has no import-time side effects (rule 12).
 * Writes: nothing. No file, no database, no chain.
 * Can move funds: no -- but it is the ONLY thing standing between the open
 *        internet and POST /swap-intents/:intentId/execute, which does.
 * Mainnet-safe: yes -- pure string and buffer arithmetic, no I/O.
 *
 * WHY THIS IS A SEPARATE FILE AND NOT FOUR LINES INSIDE server.js.
 *
 * CLAUDE.md rule 10: the thing that actually decides should be the smallest,
 * most testable piece at the bottom. Before 2026-09-24 the decision lived
 * inline in server.js's `requireSharedSecret()`, which took an Express `req`
 * and threw -- so the only way to exercise it was to stand up the whole
 * server, and nothing in this repository runs a JS server in a test. Nothing
 * ever tested it, and what nobody tested was wrong in two ways at once.
 *
 * DEFECT 1, MEASURED 2026-09-24: THE ENDPOINT THAT SPENDS MONEY HAD NO CHECK.
 *
 * Counted across the two Express servers under grc-sol-swap/abstergo_exchange/,
 * one of nine routes called the old requireSharedSecret():
 *
 *     server.js               GET  /health                      no
 *     server.js               GET  /prices                      no
 *     server.js               GET  /deposit-addresses           no
 *     server.js               GET  /swap-intents/:intentId      no
 *     server.js               POST /quote/grc-to-sol            no
 *     server.js               POST /swap-intents                no
 *     server.js               POST /swap-intents/:id/verify-gridcoin   YES
 *     server.js               POST /swap-intents/:id/execute     no   <-- pays SOL
 *     services/gridcoin.js    POST /deposit                      no   <-- sendtoaddress
 *
 * `/execute` reads the intent store, checks `status === 'verified'`, and calls
 * sendSolPayout(). Anyone who could reach the port and produce an intentId
 * could trigger a signed Solana transfer out of the hot wallet. The intentId
 * is 12 random bytes, so it is not guessable -- but it is a BEARER token that
 * is handed to the browser, logged, and returned in full by an equally
 * unauthenticated GET, and "unguessable identifier" is not authentication.
 *
 * `services/gridcoin.js` POST /deposit is worse in kind and is dead in
 * practice: it calls the Gridcoin RPC `sendtoaddress` with an amount taken
 * straight from the request body, with no ceiling and no authentication at
 * all. It is gated here too rather than left as a live hole (see the note at
 * that call site about what it is and is not).
 *
 * DEFECT 2: `!==` ON A SECRET IS TIMING-ATTACKABLE.
 *
 *     if (provided !== GRIDCOIN_VERIFY_SHARED_SECRET) { ... }
 *
 * JavaScript string comparison short-circuits at the first differing byte, so
 * the time it takes to fail is a function of how many leading bytes matched.
 * An attacker who can measure the response time can recover the secret one
 * byte at a time instead of searching the whole space. `crypto.timingSafeEqual`
 * is the fix, and it requires EQUAL-LENGTH buffers -- which is itself a leak
 * if you handle unequal lengths by returning early, because then the response
 * time tells the attacker the secret's length. Hashing both sides to a fixed
 * 32 bytes first removes the length dependence entirely: SHA-256 of a
 * one-character guess and SHA-256 of a 200-character guess are both 32 bytes,
 * so every comparison takes the same path.
 *
 * WHAT THIS DELIBERATELY IS NOT. A shared secret in a header is authentication
 * for a machine-to-machine caller, not a user session. It does not identify
 * WHO is calling, it has no expiry, and it is replayable by anyone who sees
 * one request. It is the right size for the operator-driven verify/execute
 * path, which is what these routes are, and it is not a substitute for signed
 * requests if these ever become user-facing. Said here so the next reader does
 * not mistake the fix for more than it is.
 */

import crypto from 'crypto';

/**
 * The header a caller puts the shared secret in.
 *
 * ASCII, lower case, and named once so the route table and the tests cannot
 * drift from each other (rule 8). Express lowercases incoming header names, so
 * `req.get()` is case-insensitive regardless.
 */
export const SHARED_SECRET_HEADER = 'x-gridcoin-verify-secret';

/**
 * Below this many characters, a shared secret is brute-forceable by an
 * attacker who can send requests. 32 is not a magic number from anywhere in
 * particular: it is "long enough that guessing is not the cheapest attack."
 * Falling short of it is reported at startup rather than refused, because
 * refusing to boot over the LENGTH of an already-deployed secret would stop a
 * running bridge for a hazard that is real but not new (rule 16: stopping the
 * service is the operator's call, not a side effect of a hygiene pass).
 */
export const MIN_RECOMMENDED_SECRET_LENGTH = 32;

/**
 * Constant-time comparison of a caller-provided secret against the configured
 * one.
 *
 * Returns true only when both are non-empty strings with the same SHA-256.
 * Every input shape -- undefined, null, a number, an array, an empty string, a
 * wrong-length string, a nearly-correct string -- takes the same path and the
 * same time, which is the entire point.
 *
 * The SHA-256 indirection is not about hiding the secret (both values are
 * already in memory); it is about making the two buffers the same length so
 * timingSafeEqual can be called at all without a length check that leaks.
 *
 * @param {unknown} provided  whatever arrived in the header
 * @param {unknown} expected  the configured secret
 * @returns {boolean}
 */
export function sharedSecretMatches(provided, expected) {
  // A non-string or empty configured secret can never match anything. This
  // branch is NOT timing-sensitive: it depends only on the server's own
  // configuration, which the attacker cannot vary between requests.
  if (typeof expected !== 'string' || expected.length === 0) return false;

  // A non-string provided value is normalized to the empty string rather than
  // returned on early, so that "header absent" and "header wrong" cost the
  // same. An attacker learns nothing from either.
  const providedString = typeof provided === 'string' ? provided : '';

  const providedDigest = crypto.createHash('sha256').update(providedString, 'utf8').digest();
  const expectedDigest = crypto.createHash('sha256').update(expected, 'utf8').digest();

  return crypto.timingSafeEqual(providedDigest, expectedDigest);
}

/**
 * An Error carrying an HTTP status, matching the shape server.js's route
 * handlers already expect (`error.statusCode || 400`).
 *
 * The message deliberately does not say WHICH of "no header", "wrong header"
 * or "server misconfigured" happened, for the same reason the comparison is
 * constant-time: the response body is a side channel too. The distinction is
 * available to the operator in the startup banner and in the config check
 * below, where it belongs.
 */
function unauthorized() {
  const err = new Error('Unauthorized: valid shared secret required');
  err.statusCode = 401;
  return err;
}

/**
 * Throw unless the request carries the shared secret.
 *
 * Callers use it as the first statement in a route handler, inside the
 * existing try/catch, so the 401 flows out through the handler's error path
 * exactly as the old inline version did.
 *
 * @param {{get: (name: string) => string | undefined}} req  an Express request
 * @param {{enforcementEnabled: boolean, secret: string | null}} config
 */
export function requireSharedSecret(req, config) {
  // Enforcement off is a configuration the operator chose, and it is reported
  // in the startup banner and refused outright when payouts are armed (see
  // describeSharedSecretConfig). Honoring it here without comment would make
  // the route look authenticated when it is not, which is rule 14's "did
  // nothing must not look like did work" applied to a security check.
  if (!config.enforcementEnabled) return;

  if (typeof config.secret !== 'string' || config.secret.length === 0) {
    // Fail CLOSED. The old code threw a plain Error here, which server.js's
    // handler turned into a 400 -- indistinguishable to the caller from a bad
    // request body. It is a 500: the server is misconfigured, the caller did
    // nothing wrong, and no retry with different input will help.
    const err = new Error(
      'Server misconfigured: GRIDCOIN_VERIFY_SHARED_SECRET is unset while enforcement is enabled',
    );
    err.statusCode = 500;
    throw err;
  }

  if (!sharedSecretMatches(req.get(SHARED_SECRET_HEADER), config.secret)) {
    throw unauthorized();
  }
}

/**
 * True when the request carries a valid secret, without throwing.
 *
 * This exists for ONE caller: GET /health, which answers an unauthenticated
 * liveness probe with a minimal body and the full diagnostic body only to an
 * authenticated one. Anywhere else, use requireSharedSecret -- a boolean that
 * a handler can forget to branch on is a worse shape than an exception that
 * cannot be ignored.
 */
export function hasValidSharedSecret(req, config) {
  if (!config.enforcementEnabled) return true;
  return sharedSecretMatches(req.get(SHARED_SECRET_HEADER), config.secret);
}

/**
 * Decide, at startup, whether this configuration is safe to serve -- and say
 * why in words the operator can read off the screen (rule 14).
 *
 * Returns `{ fatal, lines }`. `fatal` is a string when the process must not
 * start; `lines` is always printed.
 *
 * THE ONE FATAL COMBINATION, and why it is fatal rather than a warning:
 *
 *     REQUIRE_GRIDCOIN_SHARED_SECRET=false  AND  a payer keypair is loaded
 *
 * With enforcement off, POST /swap-intents/:intentId/execute is reachable by
 * anyone who can open a socket to the port, and with a payer keypair loaded
 * that endpoint signs and broadcasts a Solana transfer. An on-chain transfer
 * is final (CLAUDE.md, "What this system is"): there is no exchange to call
 * and no counterparty to unwind it with. A warning printed into a log that
 * nobody is watching is not a control over an irreversible action.
 *
 * `armedDescription` is what the refusal names as the exposed capability. It
 * is the caller's, because only the caller knows which routes it serves.
 *
 * With NO payer keypair, /execute already returns 501 and the same
 * configuration is merely unwise, not dangerous, so it warns instead. That
 * asymmetry is deliberate: it lets someone run the bridge read-only for
 * development without the secret plumbing, which is the legitimate reason the
 * off switch exists.
 *
 * The default is checked too, and it is safe: server.js disables enforcement
 * only on the exact lower-cased string 'false', so an unset variable, a typo,
 * `0`, `no`, or an empty value all enforce. Fail-closed by construction. That
 * is worth stating because the obvious `Boolean(process.env.X)` spelling of
 * the same idea fails OPEN on an unset variable.
 */
export function describeSharedSecretConfig({ enforcementEnabled, secret, payoutEnabled, armedDescription }) {
  // What this process can SPEND, named by the caller, because the two servers
  // in this directory are armed in different ways and a message that names the
  // wrong one is a wrong message -- which is a bug, with the same seriousness
  // as wrong code (rule 16). server.js says "POST /swap-intents/:intentId/
  // execute -- a signed, final Solana transfer"; services/gridcoin.js says
  // "POST /deposit -- a Gridcoin sendtoaddress". Measured by running both:
  // before this argument existed, gridcoin.js refused to start with a message
  // naming an endpoint it does not have.
  const armed = armedDescription || 'an endpoint that moves funds';
  const lines = [];
  const secretLength = typeof secret === 'string' ? secret.length : 0;

  if (!enforcementEnabled) {
    lines.push(
      'shared secret enforcement: DISABLED  <- REQUIRE_GRIDCOIN_SHARED_SECRET=false; every route below is open to anyone who can reach this port',
    );
    if (payoutEnabled) {
      return {
        fatal:
          `REQUIRE_GRIDCOIN_SHARED_SECRET=false while this process is armed would expose ${armed} ` +
          'to unauthenticated callers, and an on-chain transfer is final. Refusing to start. ' +
          'Set REQUIRE_GRIDCOIN_SHARED_SECRET=true and GRIDCOIN_VERIFY_SHARED_SECRET, or run a ' +
          'process that cannot move funds (for server.js, that means leaving SOLANA_PAYER_KEYPAIR_PATH unset).',
        lines,
      };
    }
    lines.push(
      'this process is NOT armed (no payer keypair), so no funds can move; the configuration is unwise but not dangerous',
    );
    return { fatal: null, lines };
  }

  if (secretLength === 0) {
    return {
      fatal:
        'REQUIRE_GRIDCOIN_SHARED_SECRET is enabled but GRIDCOIN_VERIFY_SHARED_SECRET is unset. ' +
        'Every authenticated route would answer 500. Refusing to start.',
      lines,
    };
  }

  // The LENGTH is not the secret, and printing it is what lets an operator see
  // a truncated or accidentally-empty value without reading the value back
  // (chain-safety rules: never read a key back into a terminal).
  lines.push(`shared secret enforcement: ENABLED  (configured secret length ${secretLength} chars)`);
  if (secretLength < MIN_RECOMMENDED_SECRET_LENGTH) {
    lines.push(
      `shared secret is shorter than ${MIN_RECOMMENDED_SECRET_LENGTH} chars  <- brute-forceable; rotate to a longer one. Not fatal, because stopping a running bridge over this is the operator's call.`,
    );
  }
  return { fatal: null, lines };
}
