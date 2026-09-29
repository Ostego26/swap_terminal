#!/usr/bin/env python3
"""chain_balances.py refuses a mainnet daemon BEFORE it asks about money.

Role: tests (read-only)
Reads: chain_balances.py's source and its functions, against stub adapters that
        record calls. No chain, no network, no daemon.
Writes: nothing
Can move funds: no
Mainnet-safe: yes

THE ONE THAT MATTERS is the ORDER. A balance reader that asks a mainnet Gridcoin
wallet for its balance and then discards the answer has still touched the
operator's live staking wallet, so "refuses to report it" is not the property to
test -- "never asked it" is. The stub adapter records every method, and the
mainnet case asserts the recorded list contains no wallet call at all.

The rest holds what the module header claims: the methods it may use, and that
its chain list is derived from the allowlist rather than spelled a second time.
"""

from __future__ import annotations

import ast
import pathlib
import sys

import pytest
from chains.daemon_network import CHAIN_TEST_NETWORKS
from regtest.daemons import CHAIN_DEFAULTS

SOURCE = pathlib.Path(__file__).resolve().parent.parent / "chain_balances.py"
TREE = ast.parse(SOURCE.read_text())

sys.path.insert(0, str(SOURCE.parent))

# The path insert above has to run first: chain_balances.py lives at the project
# root, which conftest.py does not put on sys.path (it adds swap_terminal/).
import chain_balances  # noqa: E402 -- checked: the sys.path.insert above is what makes this importable, and moving it earlier would import the module before its own directory is on the path. Same idiom and same reason as tests/test_xrp_chain_check_units.py:28.

# Every RPC method this script may call. All five are reads, and getwalletinfo is
# reached through chains/wallet_lock.encryption_state() rather than directly.
READ_ONLY_METHODS = frozenset({"getblockchaininfo", "getinfo", "getblockcount",
                               "getbalance", "getbalances", "getwalletinfo"})

# Anything whose presence would contradict "Can move funds: NO".
FORBIDDEN_CALLS = ("send_to_address", "sendtoaddress", "walletpassphrase", "unlock_for_sending",
                   "signrawtransactionwithwallet", "sendrawtransaction", "dumpprivkey",
                   "createhtlc", "unlocked_for_payout",
                   # ADDED 2026-09-29 with the conf fallback, which imports from
                   # regtest.daemons -- a module whose other half STARTS AND STOPS
                   # DAEMONS. The import is a constant (CHAIN_DEFAULTS) and these
                   # names are what would turn it into something else. The live
                   # rules are explicit that this harness never starts or stops a
                   # daemon.
                   "start_daemon", "_spawn", "wait_for_rpc", "apply_mweb_override")

# THE METHODS A WALLET CALL LOOKS LIKE. Separate from the allowlist above because
# the mainnet test needs to assert that NONE of these was asked, and
# getblockchaininfo -- which is how the network is established -- is not one.
WALLET_METHODS = frozenset({"getbalance", "getbalances", "getwalletinfo"})


class _Recorder:
    """A Console that keeps every line instead of printing it."""

    def __init__(self):
        self.lines = []

    def say(self, text):
        self.lines.append(text)

    def check(self, label, got, expected, ok):
        # step_console.Console.check's real signature. A recorder with fewer
        # arguments would silently accept a call the real Console rejects.
        self.lines.append(f"CHECK {label} got={got} expected={expected} ok={ok}")
        return ok

    def text(self):
        return "\n".join(self.lines)


class _Adapter:
    """A daemon that answers a fixed script and REMEMBERS what it was asked."""

    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    def call(self, method, *_params):
        self.asked.append(method)
        if method not in self.answers:
            raise RuntimeError(f"stub daemon has no answer for {method}")
        return self.answers[method]


TESTNET_DAEMON = {
    "getblockchaininfo": {"chain": "regtest"},
    "getblockcount": 677,
    "getbalance": 49.87654321,
    "getbalances": {"mine": {"trusted": 49.87654321, "immature": 5000.0,
                             "untrusted_pending": 0.5}},
    # No `unlocked_until` key: chains/wallet_lock treats its PRESENCE as the test
    # for an encrypted wallet, because bitcoin-derived daemons omit it entirely on
    # an unencrypted one rather than reporting a value.
    "getwalletinfo": {"walletname": "swap"},
}


def test_a_MAINNET_daemon_is_never_asked_about_a_balance_at_all():
    """THE ORDER IS THE PROPERTY. Refusing to print a reading is not enough.

    The operator's Gridcoin mainnet wallet is a live staking wallet. A reader
    that polls it and then declines to show the number has still polled it, which
    is what swap_readiness.py's header means by "looking is itself the hazard".

    MUTATION: move the network check below the getbalance call and this fails on
    the recorded method list. Verified 2026-09-29.
    """
    adapter = _Adapter({**TESTNET_DAEMON, "getblockchaininfo": {"chain": "main"}})
    recorder = _Recorder()
    ok = chain_balances.report_chain(recorder, "GRC", {"GRC": adapter})

    assert ok is False, "a mainnet daemon must not count as a chain that reported"
    assert not WALLET_METHODS.intersection(adapter.asked), (
        f"a mainnet daemon was asked {adapter.asked}. Nothing in that list may touch the wallet -- "
        f"the network has to be established from getblockchaininfo alone, before any balance call"
    )
    assert "REFUSED" in recorder.text(), recorder.text()


def test_an_UNREADABLE_network_is_refused_rather_than_assumed_to_be_testnet():
    """Fail closed. chain_network() returns "unknown (...)" and that is not in any allowlist."""
    adapter = _Adapter({"getbalance": 1.0})  # neither getblockchaininfo nor getinfo answers
    recorder = _Recorder()
    ok = chain_balances.report_chain(recorder, "BTC", {"BTC": adapter})

    assert ok is False
    assert not WALLET_METHODS.intersection(adapter.asked), (
        f"a daemon that would not say which network it is on was asked {adapter.asked}"
    )
    assert "unknown" in recorder.text(), recorder.text()


def test_a_test_network_daemon_reports_BOTH_halves_of_the_balance():
    """Spendable alone reads as a loss on a wallet with immature coins.

    fund_testnets.py prints the same two figures for the same reason: "balance
    0.00076293" after mining 101 blocks looks like a failure until the immature
    column sits beside it.

    MUTATION: print only the spendable line and this fails on "immature".
    Verified 2026-09-29.
    """
    adapter = _Adapter(TESTNET_DAEMON)
    recorder = _Recorder()
    ok = chain_balances.report_chain(recorder, "LTC", {"LTC": adapter})
    out = recorder.text()

    assert ok is True, out
    assert "49.87654321" in out, out
    assert "immature" in out and "5000.00000000" in out, out
    assert "pending" in out and "0.50000000" in out, out
    # The total is printed because the reader should not have to add three
    # numbers to learn what the wallet holds.
    assert "5050.37654321" in out, f"the total of the three is not printed:\n{out}"
    assert "not encrypted" in out, out


def test_an_older_daemon_with_no_getbalances_says_so_instead_of_printing_zero():
    """Gridcoin has no getbalances. 0.00000000 immature would be a measurement it never made.

    MUTATION: default immature to 0.0 and print it unconditionally, and this
    fails -- "not reported" disappears and a fabricated zero takes its place.
    Verified 2026-09-29.
    """
    adapter = _Adapter({"getblockchaininfo": {"chain": "testnet"}, "getblockcount": 3296544,
                        "getbalance": 3862.76944485, "getwalletinfo": {"unlocked_until": 0}})
    recorder = _Recorder()
    ok = chain_balances.report_chain(recorder, "GRC", {"GRC": adapter})
    out = recorder.text()

    assert ok is True, out
    assert "3862.76944485" in out, out
    assert "not reported" in out, f"an absent getbalances must say so, not print 0:\n{out}"
    assert "immature  0.00000000" not in out, (
        f"a zero was printed for a figure no daemon reported:\n{out}"
    )
    # unlocked_until PRESENT means encrypted, whatever its value. 0 is a locked
    # wallet, not an absent field.
    assert "ENCRYPTED" in out and "passphrase" in out, out


def test_an_unconfigured_chain_names_the_variable_that_is_missing():
    """Rule 14: "no adapter" is useless; which environment variable is actionable."""
    recorder = _Recorder()
    ok = chain_balances.report_chain(recorder, "BTC", {})
    assert ok is False
    assert "BTC_RPC_PORT" in recorder.text(), recorder.text()


@pytest.mark.parametrize("forbidden", FORBIDDEN_CALLS)
def test_the_balance_reader_CALLS_nothing_that_could_move_money_or_unlock_a_wallet(forbidden):
    """The header's "Can move funds: NO", checked by AST rather than by grep.

    A text search would match this file's own documentation, which names every
    one of these while saying it does not call them -- the mistake made and fixed
    earlier on 2026-09-29 in a hygiene test that searched modules/script_leg.py
    for "createhtlc" and matched the prose explaining why it is absent.
    """
    called = set()
    for node in ast.walk(TREE):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name):
                called.add(target.id)
            elif isinstance(target, ast.Attribute):
                called.add(target.attr)
    assert forbidden not in called, (
        f"chain_balances.py calls {forbidden}(). The adapter it holds CAN send and CAN unlock; what "
        f"makes this file safe is that it does not, and that is a property of this file"
    )


def test_every_rpc_method_this_script_asks_for_is_a_READ():
    """The methods travel as string literals, where the name check cannot see them.

    MUTATION: add `adapter.call("walletpassphrase", "x", 1)` and this fails.
    Verified 2026-09-29.
    """
    asked = set()
    for node in ast.walk(TREE):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Attribute) and target.attr == "call" and node.args:
            first = node.args[0]
            assert isinstance(first, ast.Constant) and isinstance(first.value, str), (
                f"adapter.call() is given a computed method name at line {node.lineno}. Every method "
                f"this script may ask for has to be readable off the page"
            )
            asked.add(first.value)
    assert asked, "found no adapter.call() at all; this test has stopped measuring anything"
    assert asked <= READ_ONLY_METHODS, (
        f"chain_balances.py calls {sorted(asked - READ_ONLY_METHODS)}, which is not in the read-only "
        f"set {sorted(READ_ONLY_METHODS)}"
    )


def test_the_chain_list_is_DERIVED_from_the_allowlist_and_not_spelled_twice():
    """Rule 8. A hand-written second list is how a chain gets polled with no allowlist.

    Every chain this iterates must have a test-network vocabulary, because the
    network check is what stands between it and a mainnet wallet. Deriving the
    list makes a chain without one impossible rather than merely unlikely.
    """
    assert set(chain_balances.CHAINS) == set(CHAIN_TEST_NETWORKS)
    for chain in chain_balances.CHAINS:
        assert CHAIN_TEST_NETWORKS[chain], f"{chain} has an EMPTY allowlist, which refuses everything"


# ---------------------------------------------------------------------------
# THE CONF FALLBACK, added 2026-09-29 when the operator asked for LTC to be
# configured and the only routes on offer were three exports, one of them a
# password.
# ---------------------------------------------------------------------------


def test_the_ENVIRONMENT_wins_over_the_conf_so_an_explicit_setting_is_never_overridden():
    """A fallback that overrode an export is the worse half of rule 8.

    Two sources for one fact is bad; two sources where the QUIET one wins is how
    an operator points a reader at one daemon and gets another.

    THE FIRST VERSION OF THIS TEST WAS WORTHLESS and the mutation said so. It
    asserted the seeded adapter got asked and that no conf line was printed,
    which passes on any machine with no LTC conf on disk -- so it measured the
    filesystem, not the code. Swapping the order in report_chain() left it green.
    Now adapter_from_conf is replaced with something that FAILS if it is called
    at all, which is the actual property: a configured chain must not consult a
    conf, whether or not one exists.

    MUTATION: reorder to `adapter_from_conf(...) or adapters.get(chain)` and this
    fails. Verified 2026-09-29, by running it -- unlike the version before it.
    """
    def _must_not_be_called(*_args, **_kwargs):
        raise AssertionError("report_chain() consulted the conf for a chain that was configured")

    adapter = _Adapter(TESTNET_DAEMON)
    recorder = _Recorder()
    original = chain_balances.adapter_from_conf
    try:
        chain_balances.adapter_from_conf = _must_not_be_called
        ok = chain_balances.report_chain(recorder, "LTC", {"LTC": adapter})
    finally:
        chain_balances.adapter_from_conf = original

    assert ok is True, recorder.text()
    assert adapter.asked, "the configured adapter was not the one consulted"


def test_GRC_has_NO_conf_fallback_because_its_conf_is_shared_with_mainnet():
    """The one chain where guessing a connection could reach real money.

    ~/.GridcoinResearch holds one conf for mainnet and testnet both, and the
    operator's mainnet wallet is a live staking wallet. The network check would
    still refuse a mainnet answer, but the place to not make that mistake is
    before the call rather than after it.

    MUTATION: add "GRC": "testnet" to CONF_FALLBACK_NETWORK and this fails.
    Verified 2026-09-29.
    """
    assert "GRC" not in chain_balances.CONF_FALLBACK_NETWORK, (
        "GRC gained a conf fallback. Its conf does not distinguish mainnet from testnet by "
        "location, so a connection picked out of it can reach the operator's staking wallet"
    )
    assert set(chain_balances.CONF_FALLBACK_NETWORK) == {"BTC", "LTC"}


def test_every_chain_with_a_conf_fallback_has_a_datadir_in_the_harness_table():
    """Rule 8: the datadir and conf name are owned by regtest.daemons, not respelled.

    A chain listed for fallback with no CHAIN_DEFAULTS entry is a disagreement
    between two tables, and the code says so rather than reading as "no conf".
    """
    for chain in chain_balances.CONF_FALLBACK_NETWORK:
        assert chain in CHAIN_DEFAULTS, f"{chain} has no datadir or conf name to look in"
        assert CHAIN_DEFAULTS[chain]["conf_name"], f"{chain} has an empty conf name"


def test_a_chain_with_neither_route_names_BOTH_of_them():
    """Rule 14: the failure line has to name every route that was tried.

    It said only "LTC_RPC_* in the environment" after the conf route existed,
    which understates what was attempted and sends the reader to export three
    variables they may not need.
    """
    recorder = _Recorder()
    assert chain_balances.report_chain(recorder, "LTC", {}) is False
    out = recorder.text()
    assert "LTC_RPC_*" in out, out
    assert "conf" in out, f"the conf route was tried and is not mentioned:\n{out}"
