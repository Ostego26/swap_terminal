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
from chains import wallet_hint
from chains.daemon_network import CHAIN_TEST_NETWORKS
from conftest import TranscriptConsole
from regtest.daemons import CHAIN_DEFAULTS

SOURCE = pathlib.Path(__file__).resolve().parent.parent / "chain_balances.py"
TREE = ast.parse(SOURCE.read_text())

sys.path.insert(0, str(SOURCE.parent))

# The path insert above has to run first: chain_balances.py lives at the project
# root, which conftest.py does not put on sys.path (it adds swap_terminal/).
import chain_balances  # noqa: E402 -- checked: the sys.path.insert above is what makes this importable, and moving it earlier would import the module before its own directory is on the path. Same idiom and same reason as tests/test_xrp_chain_check_units.py:28.
import regtest_htlc_verify  # noqa: E402 -- checked: same path insert, same reason. Imported for WALLET_NAME only; it is a module-level constant and this module's import has no side effects.

# Every RPC method this script may call. All five are reads, and getwalletinfo is
# reached through chains/wallet_lock.encryption_state() rather than directly.
READ_ONLY_METHODS = frozenset({"getblockchaininfo", "getinfo", "getblockcount",
                               "getbalance", "getbalances", "getwalletinfo",
                               # listwalletdir READS the wallet directory. loadwallet
                               # and createwallet are deliberately NOT here: they
                               # change what the daemon has open, and this file names
                               # them for the operator instead of calling them.
                               "listwalletdir"})

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
                   "start_daemon", "_spawn", "wait_for_rpc", "apply_mweb_override",
                   # ADDED with the no-wallet hint. This file names loadwallet and
                   # createwallet in a message; calling either would make a
                   # "read-only" reader change the daemon's open wallets.
                   "loadwallet", "createwallet", "ensure_wallet")

# THE METHODS A WALLET CALL LOOKS LIKE. Separate from the allowlist above because
# the mainnet test needs to assert that NONE of these was asked, and
# getblockchaininfo -- which is how the network is established -- is not one.
WALLET_METHODS = frozenset({"getbalance", "getbalances", "getwalletinfo"})


# THE RECORDER IS conftest.TranscriptConsole, imported above. This file held one of
# three character-identical copies (the others were tests/test_xrp_balances.py and
# tests/test_atomic_swap_xrp_driver.py, whose own docstring said "Same shape as the
# other recorders here"). The comment that used to sit inside check() -- "a recorder
# with fewer arguments would silently accept a call the real Console rejects" -- is now
# an assertion rather than a comment: tests/test_step_console.py compares the shared
# recorder's signatures against the real Console's.


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
    recorder = TranscriptConsole()
    spendable = chain_balances.report_chain(recorder, "GRC", {"GRC": adapter})

    assert spendable is None, "a mainnet daemon must not count as a chain that reported"
    assert not WALLET_METHODS.intersection(adapter.asked), (
        f"a mainnet daemon was asked {adapter.asked}. Nothing in that list may touch the wallet -- "
        f"the network has to be established from getblockchaininfo alone, before any balance call"
    )
    assert "REFUSED" in recorder.text(), recorder.text()


def test_an_UNREADABLE_network_is_refused_rather_than_assumed_to_be_testnet():
    """Fail closed. chain_network() returns "unknown (...)" and that is not in any allowlist."""
    adapter = _Adapter({"getbalance": 1.0})  # neither getblockchaininfo nor getinfo answers
    recorder = TranscriptConsole()
    spendable = chain_balances.report_chain(recorder, "BTC", {"BTC": adapter})

    assert spendable is None
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
    recorder = TranscriptConsole()
    spendable = chain_balances.report_chain(recorder, "LTC", {"LTC": adapter})
    out = recorder.text()

    assert spendable is not None, out
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
    recorder = TranscriptConsole()
    spendable = chain_balances.report_chain(recorder, "GRC", {"GRC": adapter})
    out = recorder.text()

    assert spendable is not None, out
    assert "3862.76944485" in out, out
    assert "not reported" in out, f"an absent getbalances must say so, not print 0:\n{out}"
    assert "immature  0.00000000" not in out, (
        f"a zero was printed for a figure no daemon reported:\n{out}"
    )
    # unlocked_until PRESENT means encrypted, whatever its value. 0 is a locked
    # wallet, not an absent field.
    assert "ENCRYPTED" in out and "passphrase" in out, out


class _AccountShadowed(_Adapter):
    """A pre-0.17 daemon where a bare `getbalance` skips account-assigned outputs.

    THE OPERATOR'S HOST, 2026-10-08, measured after a 1,988.99751357 GRC transfer
    landed with 3 confirmations:

        getbalance ()          11.00248643
        getbalance ("*", 0)    2000.0
        listunspent 0          2000.0 total, the new output present and spendable

    The stub above answers by METHOD NAME alone, which is why this subclass exists:
    the whole defect is that one method returns two different numbers depending on
    its first argument, and a fixture that cannot express that would have reported
    agreement -- which is what the suite did before this class was written.

    IT MODELS THE EFFECT AND NOT A CAUSE, deliberately. Two explanations were
    refuted the same evening -- account assignment, and the documented minconf of 1 --
    and shadow_note()'s docstring records both. A fixture built around either would
    have pinned a story rather than the behavior, and the behavior is the part that
    is measured: these two calls can disagree, and the tool must say so.
    """

    def __init__(self, bare: float, whole, answers=None):
        super().__init__(dict(answers or {
            "getblockchaininfo": {"chain": "testnet"},
            "getblockcount": 3304792,
            "getwalletinfo": {"unlocked_until": 1822988493},
        }))
        self.answers["getbalance"] = bare
        self._whole = whole

    def call(self, method, *params):
        if method == "getbalance" and params and params[0] == "*":
            self.asked.append(method)
            if self._whole is None:
                raise RuntimeError("Accounting API is deprecated (rpc code -1)")
            return self._whole
        return super().call(method, *params)


def test_a_wallet_holding_more_than_getbalance_admits_says_so_loudly():
    """The defect that made a 2000 GRC wallet read as 11, 2026-10-08.

    The operator's 1,988.99751357 GRC had THREE confirmations and was in
    `listunspent`, and the screen still said 11.00248643. Eleven blocks later the
    bare call read 2000.0 on its own.

    WHAT IS ASSERTED HERE IS THE EFFECT, because the cause is not established and two
    of my explanations for it were refuted within the hour (see shadow_note()). The
    alarm must name the discrepancy, the amount, and the CONSEQUENCE -- the number
    matters to a decision made elsewhere: chains/base.get_balance() makes the bare
    call and payout_service.refresh_wallet_inventory() stores it as hot_confirmed
    every 60s, so the desk refuses payouts and fee sweeps it could fund.

    AND IT MUST SAY THE WAIT RESOLVES IT, which is the one thing a reader can act on:
    an hour went into looking for coins that were already there.

    MUTATION: compare with `==` instead of a tolerance, or drop the alarm entirely.
    Dropping it fails here on the "HOLDS MORE" line. Verified 2026-10-08.
    """
    adapter = _AccountShadowed(11.00248643, 2000.0)
    recorder = TranscriptConsole()
    held = chain_balances.report_chain(recorder, "GRC", {"GRC": adapter})
    out = recorder.text()

    assert "HOLDS MORE THAN `getbalance` ADMITS" in out, out
    assert "1988.99751357" in out, f"the shadowed amount itself must be named:\n{out}"
    assert "2000.00000000" in out, out
    assert "hot_confirmed" in out, f"the alarm must name what reads the smaller figure:\n{out}"
    assert "listunspent 0" in out, f"and how to confirm it independently:\n{out}"
    assert "resolves itself as the deposit confirms" in out, (
        f"the one actionable fact -- waiting fixes it -- must be on the screen:\n{out}"
    )
    assert "NOT established" in out, (
        f"the cause is unknown and the alarm must not invent one; two explanations were refuted "
        f"on the day this was written:\n{out}"
    )
    assert "ACCOUNT" not in out, "the refuted account explanation must not be asserted to a reader"
    # THE RETURNED FIGURE IS WHAT THE WALLET HOLDS. say_what_levels_them() asks what
    # it would take to reach a target, and returning the bare figure would have said
    # the desk needed another 1,988.99 GRC while that exact amount sat in it.
    assert held is not None
    assert float(held) == pytest.approx(2000.0)


def test_two_agreeing_readings_print_no_alarm_at_all():
    """Every chain where the accounts idiom does not apply must stay quiet.

    An alarm on a wallet that is fine is the cried-wolf noise this file has already
    fixed once, for XRP's get_balance().
    """
    adapter = _AccountShadowed(49.87654321, 49.87654321)
    recorder = TranscriptConsole()
    chain_balances.report_chain(recorder, "LTC", {"LTC": adapter})
    out = recorder.text()
    assert "HOLDS MORE" not in out, out
    assert "shadowed" not in out, out


def test_an_unreadable_second_opinion_says_so_rather_than_claiming_agreement():
    """Rule 14: a missing second reading is a missing reading, not a clean bill.

    MUTATION: return (bare, "") when the "*" form raises. This fails -- the output
    would claim agreement it never established, which is the fail-open direction.
    """
    adapter = _AccountShadowed(11.00248643, None)
    recorder = TranscriptConsole()
    held = chain_balances.report_chain(recorder, "GRC", {"GRC": adapter})
    out = recorder.text()
    assert "whole     not reported" in out, out
    assert "HOLDS MORE" not in out, "nothing was established, so nothing is alleged"
    # THE None GUARD BEFORE THE float(), and it is the assertion this test was
    # missing rather than a formality. report_chain() returns `Decimal | None`:
    # None for every chain it could not read. The whole point of this case is
    # that an unreadable SECOND opinion must not cost the FIRST one, so a None
    # here is precisely the regression under test -- and without this line that
    # regression arrives as `TypeError: float() argument must be a string or a
    # real number, not 'NoneType'` from the line below, which names neither the
    # chain, the figure, nor what was expected. A test whose guarded failure is
    # a stack trace has stopped reporting the thing it measures.
    #
    # Its sibling above (the two-figures-disagree case) already asserts this;
    # this one did not, which is rule 8's drift between two copies of one check.
    assert held is not None, (
        f"report_chain() returned None for a daemon whose BARE getbalance answered. An unreadable "
        f"`getbalance \"*\"` costs the comparison and must not cost the reading:\n{out}"
    )
    assert float(held) == pytest.approx(11.00248643), (
        "with no second reading the only figure available is the bare one"
    )


def test_an_unconfigured_chain_names_the_variable_that_is_missing():
    """Rule 14: "no adapter" is useless; which environment variable is actionable."""
    recorder = TranscriptConsole()
    spendable = chain_balances.report_chain(recorder, "BTC", {})
    assert spendable is None
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
    recorder = TranscriptConsole()
    original = chain_balances.adapter_from_conf
    try:
        chain_balances.adapter_from_conf = _must_not_be_called
        spendable = chain_balances.report_chain(recorder, "LTC", {"LTC": adapter})
    finally:
        chain_balances.adapter_from_conf = original

    assert spendable is not None, recorder.text()
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
    recorder = TranscriptConsole()
    assert chain_balances.report_chain(recorder, "LTC", {}) is None
    out = recorder.text()
    assert "LTC_RPC_*" in out, out
    assert "conf" in out, f"the conf route was tried and is not mentioned:\n{out}"


# ---------------------------------------------------------------------------
# THE DOWN-DAEMON CASE, added 2026-09-29 after the operator ran the same read
# twice and got the same non-answer both times.
# ---------------------------------------------------------------------------


class _Unreachable:
    """A daemon that is not there. requests raises before any RPC is answered."""

    def __init__(self):
        self.asked = []

    def call(self, method, *_params):
        self.asked.append(method)
        raise ConnectionError(f"nothing is listening ({method})")


def test_a_daemon_that_is_NOT_RUNNING_gets_the_command_that_starts_it():
    """Rule 14: describe the next step, not only the state.

    "REFUSED to ask this daemon about a balance" beside "unknown
    (getblockchaininfo: ConnectionError)" is accurate and leaves the reader
    holding nothing to do. The operator re-ran the identical command and got the
    identical output, which is what a screen that describes a state instead of a
    next step produces.

    MUTATION: drop the what_to_do_about_it() line from report_chain() and this
    fails on "litecoind". Verified 2026-09-29.
    """
    adapter = _Unreachable()
    recorder = TranscriptConsole()
    spendable = chain_balances.report_chain(recorder, "LTC", {"LTC": adapter})
    out = recorder.text()

    assert spendable is None
    assert not WALLET_METHODS.intersection(adapter.asked), (
        f"a daemon that is not answering was still asked {adapter.asked}"
    )
    assert "nothing is listening" in out and "no LTC daemon is running" in out, out
    assert "litecoind" in out and "-regtest" in out and "-daemon" in out, (
        f"the command that starts it is the actionable part and is absent:\n{out}"
    )
    assert "will NOT start it for you" in out, (
        f"printing a start command without saying it is not run invites the reader to expect it "
        f"already happened:\n{out}"
    )


def test_a_daemon_that_ANSWERED_with_a_wrong_network_is_NOT_told_to_start():
    """The mainnet case. "Start it" would be wrong -- it is running.

    Conflating the two is how an operator gets told to launch a second daemon
    against a datadir the first one already holds.

    MUTATION: return the start command unconditionally and this fails on
    "ANSWERED". Verified 2026-09-29.
    """
    line = chain_balances.what_to_do_about_it("LTC", "main")
    assert "ANSWERED" in line and "it is running" in line, line
    assert "litecoind" not in line, f"a running daemon must not be told to start:\n{line}"


def test_a_chain_with_no_known_datadir_says_what_it_can_rather_than_nothing():
    """GRC has no conf fallback, so no start command is known for it.

    It still gets a sentence. "(none)" and a state with no action are the two
    shapes rule 14 forbids, and an unknown datadir is not a reason to print
    neither.
    """
    line = chain_balances.what_to_do_about_it("GRC", "unknown (getblockchaininfo: ConnectionError)")
    assert "no GRC daemon is running" in line, line
    assert "GRC_RPC_PORT" in line, f"the one thing the reader can change is not named:\n{line}"
    assert "gridcoinresearchd" not in line, (
        f"a start command was printed for the chain deliberately excluded from the conf "
        f"fallback:\n{line}"
    )


# ---------------------------------------------------------------------------
# NO WALLET LOADED, which is what a freshly started daemon answers. Since
# Bitcoin Core 0.21 it does not create a default wallet either, so this is the
# normal state of a daemon somebody just launched -- the operator hit it on LTC
# within a minute of starting litecoind, 2026-09-29.
# ---------------------------------------------------------------------------

NO_WALLET_ERROR = ("No wallet is loaded. Load a wallet using loadwallet or create a new one with "
                   "createwallet. (Note: A default wallet is no longer automatically created) "
                   "(rpc code -18)")


class _NoWalletLoaded:
    """A running daemon with nothing open. Answers chain RPCs, refuses wallet ones."""

    def __init__(self, on_disk):
        self.on_disk = on_disk
        self.asked = []

    def call(self, method, *_params):
        self.asked.append(method)
        if method == "getblockchaininfo":
            return {"chain": "regtest"}
        if method == "getblockcount":
            return 2504
        if method == "listwalletdir":
            return {"wallets": [{"name": name} for name in self.on_disk]}
        raise RuntimeError(NO_WALLET_ERROR)


def test_a_daemon_with_no_wallet_loaded_NAMES_the_wallets_it_could_load():
    """Rule 14 again, one layer in from the down-daemon hint.

    The daemon's own -18 message names loadwallet and createwallet, which is most
    of the answer and not the part that says WHICH wallet. The operator re-ran the
    read twice against that message, the same way they had against the
    ConnectionError one.

    MUTATION: drop the which_wallets_are_on_disk() call from report_chain() and
    this fails on the wallet name. Verified 2026-09-29.
    """
    adapter = _NoWalletLoaded(["regtest_htlc_harness", "other"])
    recorder = TranscriptConsole()
    spendable = chain_balances.report_chain(recorder, "LTC", {"LTC": adapter})
    out = recorder.text()

    assert spendable is None
    assert "2 wallet(s) on disk" in out, out
    assert "regtest_htlc_harness" in out, out
    assert "loadwallet" in out and "litecoin-cli" in out, (
        f"the command that loads one is the actionable part:\n{out}"
    )
    # SCOPED TO THE HINT, not to the whole transcript. The first version asserted
    # "createwallet" was absent from `out` and failed on the DAEMON'S OWN error
    # message, which names both commands -- so it was measuring the daemon's
    # wording, not this file's advice.
    hint = wallet_hint.which_wallets_are_on_disk(adapter, "LTC")
    assert "createwallet" not in hint, (
        f"a daemon WITH wallets on disk should be told to load one, not to create another:\n{hint}"
    )
    assert "'" not in hint, (
        f"the command is meant to be pasted, and Python's repr quotes do not belong in it:\n{hint}"
    )


def test_a_daemon_with_an_EMPTY_wallet_directory_is_told_to_create_one():
    """The other branch, and "(none)" is a result rather than a blank.

    "loadwallet one of []" would be nonsense, so the two cases say different
    things -- which is the distinction rule 14 asks for between a real zero and
    nothing to report.
    """
    adapter = _NoWalletLoaded([])
    recorder = TranscriptConsole()
    chain_balances.report_chain(recorder, "LTC", {"LTC": adapter})
    out = recorder.text()

    assert "NO wallet on disk" in out and "(none)" in out, out
    assert "createwallet" in out and wallet_hint.DEFAULT_WALLET_NAME in out, out
    assert "will be empty until" in out, (
        f"creating a wallet does not produce coins, and a reader who expects a balance next "
        f"should be told:\n{out}"
    )


def test_the_wallet_name_suggested_is_the_one_the_HTLC_HARNESS_uses():
    """Rule 8: one name for one thing.

    An operator who follows this hint should end up with the wallet
    regtest_htlc_verify.py then finds already loaded, not a second one beside it.
    """
    assert wallet_hint.DEFAULT_WALLET_NAME == regtest_htlc_verify.WALLET_NAME


def test_an_unlistable_wallet_directory_still_names_a_command():
    """A daemon built without wallet support has no listwalletdir.

    The names are unavailable; the reader must not be left with nothing, which is
    what returning "" or raising would do.
    """
    class _NoListing(_NoWalletLoaded):
        def call(self, method, *params):
            if method == "listwalletdir":
                raise RuntimeError("Method not found (rpc code -32601)")
            return super().call(method, *params)

    line = wallet_hint.which_wallets_are_on_disk(_NoListing([]), "LTC")
    assert "could not list" in line and "-32601" in line, line
    assert "createwallet" in line, f"no names is not a reason to name no command:\n{line}"
