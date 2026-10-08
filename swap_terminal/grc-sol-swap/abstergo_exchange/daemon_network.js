/**
 * Which network a Gridcoin daemon is on, and whether it is safe to spend from.
 *
 * Role: submodule -> function (networkFromRpcAnswers and spendVerdict ARE the
 *       decision; neither opens a socket)
 * Reads: nothing. Both functions take RPC answers already fetched by the caller.
 * Writes: nothing
 * Can move funds: no. It is the thing that REFUSES to let something else move
 *       them, which is the opposite, and it fails closed.
 * Mainnet-safe: yes, and this module is what makes services/gridcoin.js
 *       mainnet-safe. It returns a refusal for an unreadable daemon rather
 *       than guessing, so a caller that honors the verdict refuses too.
 *
 * ---------------------------------------------------------------------------
 * WHY THIS FILE EXISTS
 * ---------------------------------------------------------------------------
 *
 * Measured 2026-10-08, by grepping every money-moving path in this repository
 * for a network check: `services/gridcoin.js` was the only one without one.
 * Its own header says so in its own words, and that sentence is the whole
 * reason this module was written:
 *
 *     Mainnet-safe: NO. There is no network selector here at all; whatever
 *            GRIDCOIN_RPC_URL points at is what gets spent from.
 *
 * Every Gridcoin send on the Python side goes through
 * swap_terminal/chains/daemon_network.py::chain_network() and refuses a daemon
 * whose network is not in an allowlist. `/deposit` asked nothing: it posted
 * `sendtoaddress` to whatever URL the environment named. The operator's
 * standing instruction for this entire repository is TESTNET ONLY, and this was
 * the one spend that could not have honored it even in principle.
 *
 * ---------------------------------------------------------------------------
 * RULE 8: THIS IS A SECOND IMPLEMENTATION, DELIBERATELY, AND HERE IS THE OTHER
 * ---------------------------------------------------------------------------
 *
 * THE OTHER COPY IS `swap_terminal/chains/daemon_network.py`. Read it before
 * changing anything here. Rule 8 says to merge two copies of one rule and let
 * the survivor own the concept -- and where they genuinely cannot be merged,
 * "the difference is the point and belongs in a comment at BOTH sites, naming
 * the other one." This is that case and this is that comment: a Node process
 * cannot import a Python module, and the alternative (shelling out to python3
 * from inside a request handler on a spend path) is worse than a named
 * duplicate in every way that matters.
 *
 * So the rule is copied, and the copy is made as mechanical as possible:
 *
 *   - the two RPC routes, in order:  getblockchaininfo.chain, then
 *     getinfo.testnet as a boolean. daemon_network.py:75 iterates exactly
 *     those two pairs, for exactly the same reason -- an older Gridcoin build
 *     has no getblockchaininfo and answers "Method not found", which is not a
 *     failure but the signal to try the next field.
 *   - the allowlist: GRC accepts "test", "testnet", "regtest", which is
 *     CHAIN_TEST_NETWORKS["GRC"] at daemon_network.py:42 character for
 *     character. It is an ALLOWLIST and not "anything that is not main",
 *     because, quoting that file: "inventing 'anything that is not main' would
 *     authorize a network none of them has ever answered. An allowlist refuses
 *     the unknown; a denylist admits it."
 *   - the old-build boolean returns the NAME "main" for false rather than an
 *     empty string, so a caller compares one vocabulary instead of branching
 *     on which RPC answered.
 *
 * WHAT IS DELIBERATELY NOT COPIED. daemon_network.py carries BTC and LTC
 * allowlists too, because Python callers pass BTC and LTC handles through it.
 * Nothing in this directory touches either chain, so copying those entries
 * would be three vocabularies maintained where one is used -- and a reader
 * would reasonably assume a Bitcoin caller existed. GRC only, and this
 * paragraph is why.
 *
 * `tests/daemon_network.test.js` pins the agreement with the Python table by
 * listing the accepted strings, so a change on one side shows up as a failing
 * test on this one rather than as a silently diverging gate.
 */

/**
 * What Gridcoin itself CALLS a network that is safe to lose coins on.
 *
 * A FROZEN ARRAY, EXPORTED, WITH THE Set KEPT PRIVATE -- and it was written
 * the other way round first, which is why this paragraph is long.
 *
 * The first version was `Object.freeze(new Set([...]))`, with a comment
 * claiming a caller could not widen the gate at runtime. That claim was false
 * and `tests/daemon_network.test.js` caught it on the first run:
 * `Object.freeze` seals an object's PROPERTIES, and a Set holds its members in
 * internal slots that no property descriptor covers. `frozenSet.add('main')`
 * succeeds silently, and the gate it guards then permits mainnet.
 *
 * That is this codebase's recurring defect in miniature -- a guard that reads
 * like a guard, is named like a guard, and enforces nothing -- so it is
 * recorded rather than quietly corrected. A frozen array refuses `push` with a
 * TypeError in module scope (ES modules are strict), and `add` is not a
 * function on it at all, so the two ways a caller might reach for the gate
 * both fail loudly instead of one of them working.
 *
 * Membership is still a Set operation: _TEST_NETWORK_SET below is built once
 * from the frozen array and never exported, so there is one list and no way to
 * edit it.
 */
export const GRC_TEST_NETWORKS = Object.freeze(['test', 'testnet', 'regtest']);

/** Membership, built once from the exported list. Private on purpose -- see above. */
const _TEST_NETWORK_SET = new Set(GRC_TEST_NETWORKS);

/**
 * The prefix returned when the daemon named no network at all.
 *
 * Named rather than spelled twice for the same reason daemon_network.py names
 * its UNKNOWN_PREFIX: two readers need to tell "the daemon did not answer"
 * apart from "the daemon is on a network called something", and rule 14's
 * "did nothing must not look like did work" applies to a refusal message as
 * much as to a status line.
 */
export const UNKNOWN_PREFIX = 'unknown (';

/** Did the daemon actually name its network, or is this the failure string? */
export function isNamed(network) {
  return typeof network === 'string' && !network.startsWith(UNKNOWN_PREFIX);
}

/**
 * The network name, from RPC answers the caller already has.
 *
 * TAKES ANSWERS, NOT A CONNECTION, so the decision is testable without a
 * daemon, a socket, or node_modules -- which is the only reason this file can
 * be tested at all in the container that wrote it (rule 10: the thing that
 * decides is the smallest piece at the bottom).
 *
 * @param {object} answers
 * @param {object|Error|null} answers.blockchainInfo  getblockchaininfo's result,
 *        or the Error it raised, or null if the caller did not try.
 * @param {object|Error|null} answers.info            getinfo's result, same.
 * @returns {string} a network name, or UNKNOWN_PREFIX + why + ')'.
 */
export function networkFromRpcAnswers({ blockchainInfo = null, info = null } = {}) {
  const reasons = [];

  // getblockchaininfo.chain -- the modern build. A truthy `chain` is the answer.
  if (blockchainInfo instanceof Error) {
    reasons.push(`getblockchaininfo: ${blockchainInfo.name || 'Error'}`);
  } else if (blockchainInfo && blockchainInfo.chain) {
    return String(blockchainInfo.chain);
  } else if (blockchainInfo !== null) {
    reasons.push('getblockchaininfo: no `chain` field');
  }

  // getinfo.testnet -- a BOOLEAN on an older build. `false` means mainnet, and
  // "main" is returned rather than "" so there is one vocabulary to compare.
  // Checked for undefined rather than truthiness on purpose: `false` is a
  // meaningful answer here and the most dangerous one to read as "missing".
  if (info instanceof Error) {
    reasons.push(`getinfo: ${info.name || 'Error'}`);
  } else if (info && info.testnet !== undefined && info.testnet !== null) {
    return info.testnet ? 'testnet' : 'main';
  } else if (info !== null) {
    reasons.push('getinfo: no `testnet` field');
  }

  return `${UNKNOWN_PREFIX}${reasons.join('; ') || 'no route answered'})`;
}

/**
 * May this process spend from the daemon on `network`?
 *
 * Returns a verdict object rather than a boolean, because a refusal that
 * cannot say WHICH condition it refused on is the one thing a refusal must not
 * do -- a lesson this repository paid for on 2026-10-07, when a Gridcoin
 * precheck blocked a correct send and named an RPC code the daemon had never
 * returned.
 *
 * @returns {{allowed: boolean, network: string, reason: string}}
 */
export function spendVerdict(network) {
  if (!isNamed(network)) {
    return {
      allowed: false,
      network,
      reason:
        `the daemon did not name its network -- ${network}. ` +
        'Refusing to spend from a daemon whose network could not be read. This is ' +
        'fail-closed on purpose: an unreadable daemon is not evidence of a testnet one.',
    };
  }
  if (_TEST_NETWORK_SET.has(network)) {
    return { allowed: true, network, reason: `network=${network} is in the GRC test allowlist` };
  }
  return {
    allowed: false,
    network,
    reason:
      `network=${network} is NOT in the GRC test allowlist ` +
      `{${GRC_TEST_NETWORKS.join(', ')}}. ` +
      'Refusing to spend. If this is a mainnet daemon, that refusal is the ' +
      'point: this repository is testnet only.',
  };
}
