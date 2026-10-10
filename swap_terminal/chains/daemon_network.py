#!/usr/bin/env python3
"""Which network a bitcoin-family daemon is on, and which answers are safe.

Role: submodule (a decision, callable with a stub adapter -- rule 10)
Reads: one RPC handle, passed in. It opens no socket of its own and knows no
        host, port or credential.
Writes: nothing
Can move funds: no. It calls two read methods and returns a string.
Dependencies: the standard library only -- __future__ and collections.abc. That is a
        CONTRACT and not an accident: stack_authority.py imports this module so
        swap_stack.py can report a daemon's network while running on the host with
        plain python3 and no installed dependency. Anything added here that is not
        stdlib breaks a tool that runs outside the container.
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

# THE ONLY IMPORT IN THIS FILE BESIDES __future__, and the module header's claim
# that it "imports nothing but __future__" is now one word out of date -- which
# matters because stack_authority.py imports this module specifically on that
# promise, to stay stdlib-only for swap_stack.py running on the host. collections.abc
# is stdlib, so the promise that MATTERS is kept; the sentence is corrected below.
from collections.abc import Mapping, Sequence

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
    # `testnet4` ADDED 2026-10-10, MEASURED OFF THE OPERATOR'S OWN DAEMON. Bitcoin Core
    # v28.1.0 reports `"chain": "testnet4"` -- a value that was in NO table here -- and
    # this allowlist is the refusal gate in chain_balances.py:321, fund_desk.py:499 and
    # atomic_swap_xrp.py. So the moment their BTC daemon came up on testnet4, all three
    # tools would have refused it as an unrecognized network, and fund_desk's refusal
    # says "*** THIS IS A MAINNET DAEMON ***" -- a confidently wrong claim about a
    # testnet node, which is worse than a bare refusal.
    #
    # Fail-closed did its job: nothing read a wallet it should not have. But an
    # allowlist that refuses the network the operator was told to move to is a blocker,
    # and "it failed safely" is not the same as "it worked".
    "BTC": frozenset({"test", "testnet", "testnet4", "regtest", "signet"}),
    # LTC IS DELIBERATELY NOT GIVEN testnet4. Litecoin Core v0.21.4 derives from Bitcoin
    # Core 0.21, whose vocabulary predates testnet4 entirely -- its testnet reports
    # `"chain": "test"`, which is already here. Litecoin's DATA DIRECTORY is named
    # testnet4, which is a different thing and is exactly the kind of near-match that
    # would get added on a glance. Adding a value no daemon of this vintage can return
    # would widen an allowlist for nothing, and this one is what stands between a
    # command and a mainnet wallet.
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
    # `testnet4` ADDED 2026-10-10. Core v28.1.0 reports it and the table had no row, so
    # the report printed "which addresses this network pays is NOT ESTABLISHED -- no
    # chainparams was read for that network name" beside a daemon that was syncing
    # perfectly. That fail-closed sentence is the right one for an unknown network and
    # the wrong one for this network, which is now known.
    #
    # `tb1`, AND THE PROVENANCE MATTERS because this table's own header says the values
    # are read off chainparams.cpp rather than recalled. BIP-173 gives every Bitcoin test
    # network except regtest the hrp `tb`, and signet in this same row already shows that
    # -- signet is a separate network with its own genesis and it shares `tb` with
    # testnet3. Confirmed against the operator's own testnet4 daemon by asking it to
    # validate tb1qw7gsuq5x7wtnwtvw3kyzh22kd7c6rm00ezl9u5, a well-formed tb1 address
    # generated from this repository's own bech32 encoder.
    "BTC": {"main": "bc1", "test": "tb1", "testnet": "tb1", "testnet4": "tb1",
            "signet": "tb1", "regtest": "bcrt1"},
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


#: WHY payable_bech32_prefix() RETURNING None IS NOT ENOUGH, and this is a defect
#: that shipped and was caught by the operator's FIRST run of the report, 2026-10-09.
#:
#: That function's own docstring says "THREE OUTCOMES COLLAPSED TO TWO WOULD BE THE
#: DEFECT HERE, so read None as 'nobody has said' and never as 'it has none'".
#: stack_authority.chain_network_lines() -- written the same hour, one module over --
#: took None and printed:
#:
#:     XRP   testnet (network_id 1) no bech32 on this chain -- its addresses are
#:                                  base58, which cannot name a network
#:
#: Every clause of that is invented. XRP is not in PAYABLE_BECH32_PREFIX at all, so
#: None there means "this table knows nothing about this chain" -- not "this chain has
#: no bech32", which is GRC's answer and GRC's alone. The report stated a fact about a
#: chain the table had never been given a row for, in the confident register of the
#: lines beside it (rule 17: a reason to believe is not a measurement, and the two must
#: never be written in the same voice).
#:
#: A CLASSIFIER RATHER THAN A SENTENCE, because the four cases are a DECISION and
#: decisions belong at the bottom where they can be called with seeded inputs
#: (rule 10). The caller maps a constant to prose; it does not re-derive which case it
#: is holding. That re-derivation is what went wrong: a renderer cannot tell
#: "GRC has none" from "nobody said" once both have arrived as None.
PREFIX_KNOWN = "prefix_known"
NO_BECH32_ON_THIS_CHAIN = "no_bech32_on_this_chain"
CHAIN_NOT_IN_TABLE = "chain_not_in_table"
NETWORK_NOT_IN_TABLE = "network_not_in_table"

#: Every value bech32_prefix_status() can return, so a caller rendering one per case
#: can be TESTED for covering them all rather than discovering a gap on an operator's
#: screen. swap_stack.py already uses this shape for LISTENER_VERDICTS and
#: DOWN_VERDICTS and the comment at stack_authority.py:509 says why: "EVERY VERDICT IN
#: stack_authority.LISTENER_VERDICTS NEEDS AN ENTRY HERE."
BECH32_PREFIX_STATUSES = (
    PREFIX_KNOWN,
    NO_BECH32_ON_THIS_CHAIN,
    CHAIN_NOT_IN_TABLE,
    NETWORK_NOT_IN_TABLE,
)


def bech32_prefix_status(asset: str, network: str) -> tuple[str, str | None]:
    """Which of FOUR things is true about this chain's bech32 prefix, and the prefix if any.

    Returns `(status, prefix)` where status is one of BECH32_PREFIX_STATUSES and
    prefix is a string only when status is PREFIX_KNOWN.

      PREFIX_KNOWN             this chain uses bech32 and this network's prefix is
                               known. Say it.
      NO_BECH32_ON_THIS_CHAIN  the chain HAS a row and the row is empty, which is a
                               positive fact somebody recorded: Gridcoin has no
                               bech32 at all. Say that.
      CHAIN_NOT_IN_TABLE       no row exists. XRP, ICP and SOL are not bitcoin-family
                               chains and this table was never given a row for them.
                               SAY NOTHING ABOUT THEIR ADDRESSES -- this is the case
                               the report got wrong, and the correct amount to say
                               about a chain nobody recorded is none.
      NETWORK_NOT_IN_TABLE     the chain is known, this network name is not. Either
                               chain_network()'s "unknown (...)" sentinel, or a
                               network string no chainparams.cpp here was read for.
                               Say that it is NOT ESTABLISHED, never that there is none.

    THE TWO MIDDLE CASES BOTH ARRIVE AS None FROM payable_bech32_prefix() AND MEAN
    OPPOSITE THINGS -- "we checked and there is none" against "nobody has checked".
    That is the whole reason this function exists rather than callers testing for
    None, and CLAUDE.md rule 2's line is the general form: "I could not find a
    caller" is not "there is no caller"; say which one you established.
    """
    by_network = PAYABLE_BECH32_PREFIX.get(asset)
    if by_network is None:
        return CHAIN_NOT_IN_TABLE, None
    if not by_network:
        return NO_BECH32_ON_THIS_CHAIN, None
    prefix = by_network.get(network)
    if not prefix:
        return NETWORK_NOT_IN_TABLE, None
    return PREFIX_KNOWN, prefix


#: WHETHER A DAEMON IS CAUGHT UP, AND THE THIRD STATE IS WHY THIS IS A CLASSIFIER.
#:
#: THE GAP THIS CLOSES, surveyed 2026-10-10 across the whole tree: NOTHING reads
#: `initialblockdownload`, `verificationprogress`, or `headers` against `blocks` on
#: any Bitcoin-family chain. services/admin_view.probe_chain() reads the chain NAME
#: and returns `reachable: True` the moment the daemon answers, so every surface --
#: /admin, `swapterm chains`, the operator panel -- says REACHABLE and names the
#: right network while the node is hours from usable.
#:
#: That is not hypothetical and the cost is already written down in this tree.
#: fund_testnets.py carries a measurement whose own chain was lost: a node will not
#: "see a deposit until it has synced, and that was MEASURED at 33-54 hours on this
#: hardware." For that entire window, with the current reporting: the balance reads
#: 0, payout_service.refresh_wallet_inventory() records 0 inventory, pairs go
#: unavailable for capacity reasons with no sentence naming sync, and a customer
#: deposit is simply invisible to deposit_watcher. Nothing anywhere says "syncing".
#:
#: SO THIS IS THE SAME DEFECT CLASS AS THE OTHER TWO FOUND THE SAME DAY, and that is
#: the argument for the shape rather than for the feature:
#:
#:   reachable but on the WRONG NETWORK   the `network` field had zero readers, so
#:                                        `LTC regtest` never reached the screen
#:                                        while the operator aimed at a tltc1 address
#:   reachable but with NO WALLET         getbalance failed and a swallowed error
#:                                        printed as a value
#:   reachable but NOT SYNCED             this one
#:
#: Each is a daemon answering "yes" to the only question anybody asked it.
SYNC_SYNCED = "synced"
SYNC_BEHIND = "behind"
SYNC_NOT_ESTABLISHED = "not_established"

#: Every state sync_verdict() can return, so a caller rendering one line per state can
#: be tested for covering them all rather than discovering a gap on a screen.
SYNC_STATES = (SYNC_SYNCED, SYNC_BEHIND, SYNC_NOT_ESTABLISHED)


def sync_verdict(info: object) -> dict:
    """Is this daemon caught up? PURE -- takes a getblockchaininfo answer, opens nothing.

    Returns `{"state", "why", "blocks", "headers", "behind", "progress"}` where state
    is one of SYNC_STATES and every number is None when the daemon did not report it.

    THE DAEMON'S OWN STATEMENT FIRST. `initialblockdownload` is a boolean the node
    computes about itself, and when it is present it is the answer -- better than any
    arithmetic this function could do on heights, because the node knows things about
    its own peers' claimed tips that `headers` does not carry.

    ABSENT IS NOT SYNCED, AND THAT IS THE WHOLE REASON FOR THE THIRD STATE. Gridcoin
    is a pre-0.17 surface -- chains/daemon_capabilities.py records that its
    getblockchaininfo has no `chain` key at all, which is why chain_network() above
    needs a getinfo fallback -- so it will not carry `initialblockdownload` either.
    Defaulting a missing field to False would report the one chain that IS synced and
    working as "synced" for the right reason by accident, and would report a freshly
    built Bitcoin Core node the same way for the wrong one. So a daemon that does not
    say reads as NOT ESTABLISHED, with whatever heights it did give, and the caller
    says so rather than guessing (rule 17).

    `headers` VS `blocks` IS USED ONLY AS A COUNT, NEVER AS A VERDICT. headers ==
    blocks does not prove a node is caught up: a node 4 million blocks behind that has
    not yet fetched any headers reports 0 and 0, which is equal and is the worst
    possible moment to report "synced". So the heights are reported for the reader and
    the verdict comes from `initialblockdownload` or from nothing.
    """
    if not isinstance(info, Mapping):
        return {
            "state": SYNC_NOT_ESTABLISHED,
            "why": "no getblockchaininfo answer to read, so sync state was not established",
            "blocks": None, "headers": None, "behind": None, "progress": None,
        }
    blocks = info.get("blocks")
    headers = info.get("headers")
    progress = info.get("verificationprogress")
    behind = (
        headers - blocks
        if isinstance(blocks, int) and isinstance(headers, int) and headers >= blocks
        else None
    )
    downloading = info.get("initialblockdownload")
    numbers = {"blocks": blocks, "headers": headers, "behind": behind, "progress": progress}
    if downloading is True:
        return {
            "state": SYNC_BEHIND,
            "why": (
                "the daemon says it is still in initial block download. Until it finishes, a "
                "deposit to this chain is INVISIBLE -- the balance reads 0, wallet inventory "
                "records 0, and nothing else on any screen will name sync as the reason"
            ),
            **numbers,
        }
    if downloading is False:
        return {"state": SYNC_SYNCED, "why": "", **numbers}
    return {
        "state": SYNC_NOT_ESTABLISHED,
        "why": (
            "this daemon does not report `initialblockdownload`, so whether it is caught up is "
            "NOT established -- it is not a claim that it is. A pre-0.17 build (Gridcoin) has no "
            "such field; the heights beside this line are what it did say"
        ),
        **numbers,
    }


#: WHETHER A DAEMON MAY BE TOUCHED AT ALL, as three named outcomes rather than a bool.
#:
#: CHAIN_TEST_NETWORKS above is the allowlist and four call sites each test membership
#: of it by hand -- chain_balances.py:321, fund_desk.py:499, atomic_swap_xrp.py:1788,
#: and as of 2026-10-10 a fifth that creates wallets on the operator's testnet
#: daemons. One rule, five spellings, which is rule 8's shape; and two of the existing
#: four COLLAPSE the distinction below, which is the part that has already cost
#: something.
#:
#: THE DISTINCTION IS WHY THIS IS NOT A PREDICATE. "the daemon would not say" and "the
#: daemon said a network that is not allowed" are different facts with different
#: remedies -- the first is an RPC or credential problem, the second is a daemon
#: pointed at the wrong chain -- and only fund_desk.py told them apart. The table's
#: own comment records what collapsing them cost: fund_desk's refusal reads "*** THIS
#: IS A MAINNET DAEMON ***", and when the operator's BTC node came up on `testnet4`
#: (a value that was in no table) that sentence was a confidently wrong claim about a
#: testnet node. Fail-closed worked; the message did not.
NETWORK_IS_TEST = "network_is_test"
NETWORK_NOT_ESTABLISHED = "network_not_established"
NETWORK_NOT_ALLOWED = "network_not_allowed"

#: Every outcome, so a reader and a test have one list rather than three literals.
TEST_NETWORK_VERDICTS = (NETWORK_IS_TEST, NETWORK_NOT_ESTABLISHED, NETWORK_NOT_ALLOWED)


def test_network_verdict(asset: str, network: str) -> str:
    """May this daemon be touched? Three outcomes, and the two refusals are different.

    Pure: takes the asset and whatever chain_network() returned, and reads
    CHAIN_TEST_NETWORKS. No adapter, no socket.

      NETWORK_IS_TEST         the daemon named a network on this chain's allowlist.
      NETWORK_NOT_ESTABLISHED it did not name one at all -- chain_network() returned
                              its UNKNOWN_PREFIX sentinel. An RPC or credential
                              problem, not a wrong chain.
      NETWORK_NOT_ALLOWED     it named a network, and that network is not allowed.

    FAILS CLOSED, and the ORDER is what makes that true: NOT_ESTABLISHED is checked
    before membership, so the sentinel string can never be mistaken for a network name
    that happens not to be in a set. Only NETWORK_IS_TEST is permission; both others
    refuse, and a caller written `if verdict != NETWORK_IS_TEST: refuse` is correct
    without knowing which.

    NOT_ALLOWED RATHER THAN "IS MAINNET", and that is the over-claim this exists to
    stop repeating. For BTC the allowlist already covers signet, so a named-but-refused
    BTC network is in fact `main` -- but for LTC it is not: Litecoin Core 0.21 has
    signet too and `signet` is deliberately absent from LTC's row, so a refused LTC
    network may be signet rather than mainnet. A verdict that said MAINNET would be
    wrong on exactly the daemon a careful operator set up. Callers that want to say
    "mainnet" may; they have to look at the network to earn it.

    AN UNKNOWN ASSET REFUSES rather than raising. `CHAIN_TEST_NETWORKS[asset]` on a
    chain with no row is a KeyError three frames inside whatever was asking, and the
    honest answer for "I have no allowlist for this chain" is not permission.
    """
    if not is_named(network):
        return NETWORK_NOT_ESTABLISHED
    if network in CHAIN_TEST_NETWORKS.get(asset, frozenset()):
        return NETWORK_IS_TEST
    return NETWORK_NOT_ALLOWED


#: WHAT IS ACTUALLY LIMITING AN UNFINISHED SYNC, AND IT IS NOT USUALLY THE PEERS.
#:
#: sync_verdict() above answers "is this node caught up". It cannot answer the
#: question an operator asks next, which is "how long, and is there anything I can do
#: about it" -- and the obvious answer to the second half is wrong.
#:
#: MEASURED ON THE OPERATOR'S HOST 2026-10-10, on LTC testnet at height ~3.1M of
#: 4.9M, with ONE peer:
#:
#:     blocks validated   2153 in 30.0s   (71.7/s)
#:     bytes received     1,646,379       (0.055 MB/s)
#:     average block      0.8 kB on the wire
#:
#: 0.055 MB/s is a factor of 36 below the 2 MB/s floor below. The single peer was
#: feeding that node FASTER THAN IT COULD VALIDATE, so the thing everyone reaches for
#: -- more peers -- would have changed little. The node was CPU- and disk-bound.
#:
#: THIS EXISTS BECAUSE I GOT IT WRONG IN THAT REGISTER. A diagnostic handed to the
#: operator printed `peers now 1  <- want 8+; one peer is why the sync is slow`, and
#: "one peer is why" was an inference stated as a measurement -- rule 17's exact
#: failure, in output the operator then pasted back twice and acted on. The node had
#: 8219 known addresses, no `connect=`, no `maxconnections=`, and nothing capping it
#: on either the conf or the command line: one peer was what it had ESTABLISHED, not
#: what it was told to establish, and it was enough. The verdict is computed from a
#: stated threshold now instead of from an impression.
SYNC_NETWORK_BOUND = "network-bound"
SYNC_VALIDATION_BOUND = "validation-bound"
SYNC_NOT_ADVANCING = "not-advancing"
SYNC_RATE_NOT_ESTABLISHED = "rate_not_established"

#: TOTAL OVER THE FOUR, so a renderer can assert it covers every verdict rather than
#: inventing a sentence for one it did not know about -- which is the defect
#: stack_authority's BECH32 renderer shipped on 2026-10-09 and the reason
#: TEST_NETWORK_VERDICTS above is spelled the same way.
BOTTLENECK_VERDICTS = (
    SYNC_NETWORK_BOUND,
    SYNC_VALIDATION_BOUND,
    SYNC_NOT_ADVANCING,
    SYNC_RATE_NOT_ESTABLISHED,
)

#: THE THRESHOLD, AND IT IS A CONSTANT SO THAT THE VERDICT IS AUDITABLE. A link that
#: can carry 2 MB/s is unremarkable; a node pulling far less than that while blocks
#: keep arriving is not waiting on the network. Chosen as a round number well above
#: what a saturated IBD needs for small testnet blocks (0.8 kB each, measured above)
#: and well below any real link, so the two cases are separated by more than an order
#: of magnitude rather than by a judgement call.
#:
#: NAMED IN BYTES PER SECOND, ASCII, because it is an identifier -- rule 6's boundary.
#: The figure is RENDERED in MB/s because that is the unit a human compares a link in.
NETWORK_BOUND_FLOOR_BYTES_PER_SECOND = 2_000_000

#: TWO READINGS ARE A RATE; ONE IS A POSITION. Named rather than written as a `2`
#: in the guard, because ruff PLR2004 is right that the number needs a word next to
#: it -- and the word is the whole reason the guard exists: a caller whose daemon
#: stopped answering partway through has one sample and must get a refusal, not a
#: rate computed against itself.
MINIMUM_SAMPLES = 2

#: AND THE WINDOW HAS TO BE LONG ENOUGH TO BE A WINDOW. Found by a test 2026-10-10:
#: with a zero gap between samples the span becomes one RPC round-trip, and 1.6 MB of
#: `totalbytesrecv` over 0.0002s computes as 16 GB/s -- so a node that is firmly
#: validation-bound reports NETWORK-BOUND, confidently, with every figure arithmetically
#: correct.
#:
#: That is this module's own founding defect wearing a different hat. The verdict was
#: added because "one peer is why the sync is slow" was an inference presented as a
#: measurement; a verdict computed over a round-trip is a measurement of the wrong
#: interval presented as a rate. Both hand a reader a confident wrong cause.
#:
#: ONE SECOND, because `totalbytesrecv` moves in bursts as blocks arrive: a window
#: shorter than that can land entirely inside or entirely outside a burst, so the byte
#: rate is dominated by where the samples fell rather than by the throughput. It is a
#: parameter for the same reason floor_bps is -- so a test can drive the guard without
#: sleeping through it.
MINIMUM_WINDOW_SECONDS = 1.0


def rate_refusal(samples: object, minimum_window: float = MINIMUM_WINDOW_SECONDS) -> str:
    """Why these samples cannot produce a rate, or "" when they can.

    FOUR WAYS TO FAIL AND THEY ARE ALL THE SAME KIND, which is what makes this a seam
    rather than a split for its own sake: every one is a statement about the SAMPLES,
    none is a statement about the sync. sync_bottleneck() below is then purely about
    interpreting numbers it has been told are usable --
    swap_terminal_desktop._transport_verdict/_body_verdict draw the same line for the
    same reason, and that file says it plainly: "this half knows only about sockets,
    and the other half knows only about the response".

    EXTRACTED BECAUSE ruff PLR0911 FLAGGED THE COMBINED VERSION at 7 returns > 6, and
    rule 12's answer to a complexity finding is to extract the decision rather than
    raise the ceiling. The win is not the lint code: this function can be asserted on
    directly, so "a window of 0.3s is refused" is one call rather than a dict lookup
    through a verdict.

    IT RETURNS A SENTENCE, NOT A BOOL. A caller that only learned "unusable" would
    have to invent the reason, and the reasons are not interchangeable -- a wall clock
    that stepped backwards and a window too short to clear a burst send an operator to
    two different places.
    """
    if not isinstance(samples, Sequence) or len(samples) < MINIMUM_SAMPLES:
        return (
            "fewer than two samples, so no rate could be computed. That is a result and "
            "not a failure: it means the daemon stopped answering partway through"
        )
    try:
        (t0, _b0, _h0, _n0), (t1, _b1, _h1, _n1) = samples[0], samples[-1]
    except (TypeError, ValueError):
        return (
            "a sample was not a (elapsed_seconds, blocks, headers, bytes_received) "
            "quadruple, so nothing was computed rather than something guessed"
        )
    elapsed = t1 - t0
    if elapsed <= 0:
        return (
            f"the samples span {elapsed}s, which cannot produce a rate. A monotonic clock "
            f"was expected; a wall clock that stepped backwards reads like this"
        )
    if elapsed < minimum_window:
        return (
            f"the samples span {elapsed:.4f}s, under the {minimum_window}s minimum. A window "
            f"that short measures one RPC round-trip rather than throughput: totalbytesrecv "
            f"moves in bursts as blocks arrive, so the byte rate would say more about where "
            f"the two samples landed than about what is limiting the sync. Nothing is "
            f"reported rather than a confident wrong cause"
        )
    return ""


def sync_bottleneck(
    samples: object,
    floor_bps: float = NETWORK_BOUND_FLOOR_BYTES_PER_SECOND,
    minimum_window: float = MINIMUM_WINDOW_SECONDS,
) -> dict:
    """Which resource an unfinished sync is waiting on, from two or more samples.

    Args:
        samples: a sequence of (elapsed_seconds, blocks, headers, bytes_received).
            `elapsed_seconds` is a monotonic reading -- the caller's, because this
            function performs no I/O and takes no clock of its own, which is what
            lets a test seed it with exact numbers instead of sleeping.
        floor_bps: the bytes-per-second above which the network could plausibly be
            the ceiling. A parameter rather than a literal so a test can drive both
            branches without pretending to have a gigabit link.

    Returns:
        A dict with `state` (one of BOTTLENECK_VERDICTS), `why`, and the measured
        figures: blocks_per_second, bytes_per_second, bytes_per_block, behind,
        eta_seconds. Every figure is None when it could not be computed, never 0 --
        rule 14, because a zero that means "not measured" and a zero that means
        "zero" send a reader to two different places.

    ONLY THE FIRST AND LAST SAMPLE DECIDE, deliberately. Averaging the intervals
    pairwise would weight a momentary stall the same as the whole run, and the
    question is the throughput over the window, not its variance. The intermediate
    samples exist so the CALLER can print progress while it waits (rule 14: a
    thirty-second blank is indistinguishable from a hang), not so this can smooth
    anything.

    THE ETA IS NAMED AS AN EXTRAPOLATION rather than a prediction, and the caller is
    expected to say so. Measured twice on the operator's host twenty minutes apart,
    the rate fell from 79.8 blocks/s to 71.7 -- 6.3 hours became 7.0 -- because block
    density rises with height. A figure that moves that much in twenty minutes is a
    current rate, not a finish time.
    """
    refusal = rate_refusal(samples, minimum_window)
    if refusal:
        return {
            "state": SYNC_RATE_NOT_ESTABLISHED, "why": refusal,
            "blocks_per_second": None, "bytes_per_second": None, "bytes_per_block": None,
            "behind": None, "eta_seconds": None,
        }
    blank = {
        "state": SYNC_RATE_NOT_ESTABLISHED, "why": "",
        "blocks_per_second": None, "bytes_per_second": None, "bytes_per_block": None,
        "behind": None, "eta_seconds": None,
    }
    (t0, b0, _h0, n0), (t1, b1, h1, n1) = samples[0], samples[-1]
    elapsed = t1 - t0
    gained, got, behind = b1 - b0, n1 - n0, h1 - b1
    per_second = gained / elapsed
    bytes_per_second = got / elapsed
    figures = {
        "blocks_per_second": per_second,
        "bytes_per_second": bytes_per_second,
        "bytes_per_block": (got / gained if gained > 0 else None),
        "behind": behind,
        "eta_seconds": (behind / per_second if per_second > 0 else None),
    }
    if gained <= 0:
        # NOT ADVANCING MEANS TWO DIFFERENT THINGS and only one of them is a problem.
        # A CAUGHT-UP node does not advance either: LTC blocks arrive every ~2.5
        # minutes, so any window shorter than that sees zero on a perfectly healthy
        # node. Found by a test 2026-10-10 -- the already-synced case produced "THE
        # SYNC IS NOT ADVANCING", which is the confident wrong cause this whole
        # verdict exists to stop, aimed at a node with nothing wrong with it.
        #
        # `behind` separates them and it is already computed, so the sentence says
        # which rather than leaving the caller to check. The STATE is deliberately
        # the same: in both cases there is no rate to extrapolate, and a fifth
        # verdict would make every renderer handle a case that needs no action.
        if behind <= 0:
            return {**blank, **figures, "state": SYNC_NOT_ADVANCING, "why": (
                f"{gained} blocks in {elapsed:.1f}s, and the node is NOT behind ({behind} to "
                f"go). This is a caught-up node idling between blocks, not a stalled sync -- "
                f"there is nothing to wait for and nothing to fix"
            )}
        return {**blank, **figures, "state": SYNC_NOT_ADVANCING, "why": (
            f"{gained} blocks in {elapsed:.1f}s with {behind} still to go. THE SYNC IS NOT "
            f"ADVANCING, and that is the answer: no amount of extra peers helps a node that "
            f"is not moving, so what stalled it has to be found first"
        )}
    if bytes_per_second >= floor_bps:
        return {**blank, **figures, "state": SYNC_NETWORK_BOUND, "why": (
            f"{bytes_per_second / 1_000_000:.2f} MB/s is enough of a link that the peer set "
            f"could plausibly be the ceiling, so more peers SHOULD help"
        )}
    return {**blank, **figures, "state": SYNC_VALIDATION_BOUND, "why": (
        f"{bytes_per_second / 1_000_000:.3f} MB/s is a trickle next to the "
        f"{floor_bps / 1_000_000:.0f} MB/s floor, so the peer set is NOT the ceiling -- this "
        f"node is CPU- and disk-limited verifying signatures and writing the chainstate. "
        f"More peers would change little; dbcache is the lever, and it needs a restart"
    )}
