#!/usr/bin/env python3
"""Which network a bitcoin-family daemon is on, and which answers are safe.

Role: submodule (a decision, callable with a stub adapter -- rule 10)
Reads: one RPC handle, passed in. It opens no socket of its own and knows no
        host, port or credential.
Writes: nothing
Can move funds: no. It calls two read methods and returns a string.
Mainnet-safe: yes, and this module is what MAKES other things mainnet-safe: it
        fails closed, returning "unknown" rather than guessing, so a caller that
        compares against the allowlist below refuses an unreadable daemon.

MOVED HERE 2026-09-29 from atomic_swap_xrp.py, which is a driver that sends. A
second caller appeared -- chain_balances.py, read-only and forbidden by its own
tests from importing anything that submits -- and a second copy of "is this
daemon safe to touch" is rule 8's shape at the one place it costs the most: the
check that stands between a command and a mainnet wallet.

Nothing about either piece changed in the move except two comments that were
already wrong. chain_network()'s docstring said "Which Gridcoin network this
daemon is on" and it has been called with BTC and LTC handles since 2026-09-29;
and CHAIN_TEST_NETWORKS' own explanation had drifted three definitions away from
it in the driver, stacked with two other constants' comments above a third
constant, so every one of the four described something it did not sit beside.
"""

from __future__ import annotations

#: What each script chain CALLS a network that is safe to lose coins on. Per chain and
#: not one shared set, because the strings differ and a shared set is how a mainnet
#: answer slips through: Bitcoin says "main" for mainnet and "test"/"regtest"/"signet"
#: otherwise, Litecoin the same, and Gridcoin answers "test" or "testnet".
#:
#: NOT DERIVED FROM A COMMON RULE, deliberately. There is no rule -- these are three
#: daemons' own vocabularies, and inventing "anything that is not main" would authorize
#: a network none of them has ever answered. An allowlist refuses the unknown; a
#: denylist admits it.
#:
#: THERE IS A SECOND COPY OF THE GRC ROW, IN JAVASCRIPT, AND RULE 8 SAYS TO NAME IT
#: HERE: swap_terminal/grc-sol-swap/abstergo_exchange/daemon_network.js holds
#: GRC_TEST_NETWORKS and mirrors chain_network()'s two-route probe below. It exists
#: because services/gridcoin.js spends GRC from a Node process and, measured
#: 2026-10-08, was the only money-moving path in this repository with no network check
#: at all -- a Node process cannot import this module, and shelling out to python3
#: from inside a request handler on a spend path is worse than a named duplicate.
#:
#: The JS copy carries the GRC row ONLY, not BTC or LTC, because nothing in that
#: directory touches either chain. Its own header explains that omission, and
#: abstergo_exchange/tests/daemon_network.test.js asserts its allowlist equals this
#: one string for string, so widening or narrowing either side fails a test that
#: names the other file rather than drifting silently.
CHAIN_TEST_NETWORKS = {
    "BTC": frozenset({"test", "testnet", "regtest", "signet"}),
    "LTC": frozenset({"test", "testnet", "regtest"}),
    "GRC": frozenset({"test", "testnet", "regtest"}),
}


#: The prefix chain_network() returns when it could not read a network at all.
#:
#: NAMED rather than spelled twice, because a second reader appeared on
#: 2026-09-30: services/admin_view.probe_chain() has to render "the daemon did
#: not name its network" differently from a network called something -- rule
#: 14's "did nothing must not look like did work", applied to a table cell. The
#: existing callers never needed to ask, because they test membership of
#: CHAIN_TEST_NETWORKS and an unknown string is in no allowlist.
UNKNOWN_PREFIX = "unknown ("


def is_named(network: str) -> bool:
    """Did the daemon actually name its network, or is this the failure string?

    A predicate rather than each caller testing the prefix, so the sentinel has
    exactly one spelling and chain_network() below builds its own return value
    from the same constant (rule 8).
    """
    return not network.startswith(UNKNOWN_PREFIX)


def chain_network(adapter) -> str:
    """Which network this daemon is on, as a name. Two field names, because it moved.

    A modern build answers getblockchaininfo.chain; an older one only has
    getinfo.testnet as a boolean. Both are read and the answer is returned as a
    name, so a caller compares one string rather than branching on which RPC
    answered. An unreadable network is NOT treated as a test network -- it
    returns "unknown", and every caller refuses on it (fail closed).
    """
    reasons = []
    for method, field in (("getblockchaininfo", "chain"), ("getinfo", "testnet")):
        try:
            answer = adapter.call(method) or {}
        except Exception as error:  # noqa: BLE001 -- checked: an older daemon does not HAVE getblockchaininfo and answers "Method not found", which is not a failure here but the signal to try the next field. The reason is collected rather than discarded (no bare pass, S110) and returned in the "unknown" string, so an operator sees WHY the network could not be read. A failure of both routes returns "unknown", which every caller refuses -- fail closed, never "probably testnet".
            reasons.append(f"{method}: {type(error).__name__}")
            continue
        value = answer.get(field)
        if field == "chain" and value:
            return str(value)
        if field == "testnet" and value is not None:
            # getinfo.testnet is a BOOLEAN on an old build. False means mainnet,
            # and "main" is returned rather than "" so the caller compares one
            # vocabulary (rule 11) instead of branching on which RPC answered.
            return "testnet" if value else "main"
        reasons.append(f"{method}: no `{field}` field")
    return f"{UNKNOWN_PREFIX}{'; '.join(reasons) or 'no route answered'})"


#: WHICH BECH32 ADDRESS PREFIX a daemon on each network issues and accepts, per chain.
#: Keyed by the EXACT strings chain_network() above can return, because that is the only
#: vocabulary a caller has: `getblockchaininfo.chain` answers "main", "test", "regtest" or
#: "signet", and the `getinfo.testnet` fallback answers "testnet" or "main". All six
#: spellings appear below rather than being normalized first, so there is no second
#: mapping step to drift (rule 11: one vocabulary, applied in one place).
#:
#: WHY THIS IS A SEPARATE TABLE FROM modules/address_network.BECH32_HRPS_BY_ASSET, AND
#: RULE 8 SAYS TO NAME THE OTHER ONE HERE BECAUSE THE DIFFERENCE IS THE WHOLE POINT:
#:
#:   BECH32_HRPS_BY_ASSET   hrp -> network, and it has only TWO network values. It folds
#:                          regtest into TESTNET on purpose -- it answers "is this an
#:                          address it is safe to lose coins on", and `rltc` and `tltc`
#:                          have the same answer to that question.
#:   this table             network -> hrp, and regtest is DISTINCT. It answers "which
#:                          addresses can THIS running daemon actually pay", and `rltc`
#:                          and `tltc` have opposite answers to that one.
#:
#: Neither is a candidate to replace the other and inverting either gives the wrong
#: shape: BECH32_HRPS_BY_ASSET["LTC"] inverted maps TESTNET -> {tltc, rltc}, which is
#: exactly the collapse that makes it unable to answer this question.
#:
#: WHAT THIS COST, measured on the operator's host 2026-10-09. They had a Litecoin
#: testnet wallet at `tltc1q37khgpktccdwpxq6vmkt6gtrnra3x39tvcyx62` and a desk litecoind
#: running `-regtest` (pid 165984, `-datadir=/home/mpjones26/regtest/ltc`). Those cannot
#: transact: a regtest node issues and accepts `rltc1...` and regtest coins are not on
#: testnet. services/admin_view.probe_chain() had ALREADY READ the word "regtest" off
#: that daemon and put it in its row -- and `swap_stack.py chains`, the report the
#: operator actually runs, printed "all 3 probeable chain(s) answered: BTC, LTC, GRC"
#: and dropped the field. Only the browser panel rendered it (static/admin.js:161).
#: Several rounds went on the question the probe had already answered.
#:
#: VALUES READ OFF chainparams.cpp, NOT RECALLED (rule 17). Bitcoin's bech32_hrp is "bc"
#: on main, "tb" on both testnet and signet, and "bcrt" on regtest; Litecoin's is "ltc",
#: "tltc" and "rltc". modules/address_network.py:182-191 cites the same Litecoin file for
#: `rltc` and records what its absence cost on 2026-09-27.
#:
#: GRC IS PRESENT WITH AN EMPTY TABLE on purpose, matching BECH32_HRPS_BY_ASSET's own
#: reasoning: "Gridcoin has no bech32" is a fact a caller must be able to read off this
#: vocabulary, and an absent key would mean "nobody has said".
PAYABLE_BECH32_PREFIX: dict[str, dict[str, str]] = {
    "BTC": {"main": "bc1", "test": "tb1", "testnet": "tb1", "signet": "tb1", "regtest": "bcrt1"},
    "LTC": {"main": "ltc1", "test": "tltc1", "testnet": "tltc1", "regtest": "rltc1"},
    "GRC": {},
}


def payable_bech32_prefix(asset: str, network: str) -> str | None:
    """Which bech32 prefix a daemon of `asset` on `network` pays. None when not established.

    THREE OUTCOMES COLLAPSED TO TWO WOULD BE THE DEFECT HERE, so read None as
    "nobody has said" and never as "it has none":

      a prefix   this chain uses bech32 and this network's prefix is known.
      None       one of three things, and the caller must not claim either of the
                 others -- the asset is not a bitcoin-family chain this table
                 knows, or it is GRC which has no bech32 at all, or `network` is
                 a string chain_network() could not resolve (its "unknown (...)"
                 sentinel, or a network no chainparams.cpp here was read for).

    FAILS CLOSED BY DESIGN, exactly as CHAIN_TEST_NETWORKS above does and for the
    same reason: an allowlist refuses the unknown, a denylist admits it. A caller
    that wants to WARN about a mismatch gets None and says nothing, which is the
    correct amount to say about a network nobody named.

    BASE58 IS DELIBERATELY NOT ANSWERED HERE and the omission is the finding. The
    testnet P2PKH version byte is 0x6f on BTC testnet, BTC regtest, LTC testnet,
    LTC regtest, BTC signet AND GRC testnet -- six networks, one byte. A legacy
    `m...`/`n...` address cannot be attributed to any one of them, so a function
    returning a base58 prefix per network would be returning a guess in the same
    voice as a fact (rule 17). bech32 carries the network in the string itself,
    which is why only it can be answered.
    """
    return PAYABLE_BECH32_PREFIX.get(asset, {}).get(network) or None
