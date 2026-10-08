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
