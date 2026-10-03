#!/usr/bin/env python3
"""The desk's hot wallet was the operator's own wallet, and nothing could say so.

Role: tests (read-only)
Reads: the modules under test, and stub adapters this file constructs. No chain,
        no daemon, no socket, no real database, none of the operator's confs.
Writes: nothing
Can move funds: no
Mainnet-safe: yes -- every "daemon" here is a Python object that records the
        method names it was asked for.

THE DEFECT, 2026-10-03, in the operator's own words: "these two hot wallets are
used by the swap terminal machine NOT THE USER."

This is a CUSTODIAL desk, so a GRC->XRP swap must move coins OUT of the
customer's wallet and INTO the terminal's, and XRP OUT of the terminal's account
and INTO the customer's. Three things were measured that day:

  1. swap s_539d922e9ef0a5d8 completed. Its 500 GRC deposit went to
     moaSBv8gcwXRnmQhxJJAjUvXMd542jsNNz, derived by getnewaddress on the
     operator's OWN gridcoinresearchd, and their balance moved
     3780.08654497 -> 3780.08554497: the 0.001 fee and nothing else. The
     transaction carries `category: send` AND `category: receive` for one
     address. A self-transfer. (The operator's measurement; no network path from
     the test environment to that daemon, so it is cited, not re-run.)
  2. the XRP payout went rnjG8n16JinjqkzZj5Jmw6NDMBMzhhNbVv ->
     rBfM7je6e9Ca2cMvuRn7cr9xExFgDa5NGx, 3315589 drops, tesSUCCESS, validated.
     Both the operator's own testnet faucet accounts. (Also theirs.)
  3. MEASURED IN THIS TREE, and this is the mechanism behind 1: BTC_RPC_WALLET,
     LTC_RPC_WALLET and GRC_RPC_WALLET are all `_env(..., "")`, and
     chains/base.RPCAdapter.url appends /wallet/<name> only for a non-empty
     value -- so an unset one addresses the daemon with NO wallet path and the
     daemon routes to its DEFAULT wallet, the one a bare CLI call reaches.

WHAT THESE TESTS HOLD, AND THE ONE THING THEY REFUSE TO HOLD. The verdicts have
to differ from one another, carry their reason, and never render "could not ask"
as an answer -- that last one is the whole hazard, because a custody tool whose
default is green reports a separation it never measured. What no test here
asserts is that the desk's coins were never the operator's: no chain and no RPC
can establish it, and a test claiming otherwise would be pinning a lie.

EVERY TEST IN THIS FILE WAS MUTATION-CHECKED and each docstring names the
mutation that was run and what it caught. Where a first mutation SURVIVED, that
is recorded too, with the assertion that was added to catch it -- a surviving
mutation reported as caught is the worst outcome available here.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from chains.base import AddressOwnership
from chains.xrp_payout_seed import SIGNING_SEED_ENV_VAR, derived_payout_account, signing_seed_decodes
from network_target import CHAIN_PORTS, UNCONFIGURED_PORT, may_read_a_wallet
from report_block import LABEL_WIDTH
from services.custody_separation import (
    BY_DESIGN,
    DESK_OWNS,
    MISCONFIGURED,
    NOT_ESTABLISHED,
    NOT_SEPARATED,
    NOT_THE_DESKS,
    SEPARATED,
    STATES,
    deposit_address_verdict,
    script_chain_verdict,
    solana_account_verdict,
    wallet_label,
    what_this_cannot_establish,
    xrp_desk_account_verdict,
    xrp_payout_destination_verdict,
)
from valid_addresses import GRC_DESK_DEPOSIT, XRP_CUSTOMER_PAYOUT, XRP_HOT_ACCOUNT

import wallet_custody  # isort: skip

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: The incident's swap id, and DERIVED addresses for the three roles it involved.
#:
#: THE FIRST VERSION OF THIS BLOCK HELD THE THREE REAL ADDRESSES AS LITERALS, and
#: tests/test_address_literals_are_valid.py refused the tree at 63 against a
#: ceiling of 60 -- which was the gate working rather than an obstacle. Its own
#: message says what to do instead ("use tests/valid_addresses.py rather than
#: writing one"), and raising the ceiling would have been rule 19's forbidden move
#: on a ratchet that had exactly zero headroom before this change.
#:
#: Nothing asserted below depends on an address's VALUE -- only on the three being
#: distinct, correctly shaped for their chains, and comparable. The real addresses
#: are evidence about one run, so they live in this module's docstring, where a
#: reader meets them as what they are: moaSBv8gcwXRnmQhxJJAjUvXMd542jsNNz took the
#: 500 GRC, and the payout went rnjG8n16JinjqkzZj5Jmw6NDMBMzhhNbVv ->
#: rBfM7je6e9Ca2cMvuRn7cr9xExFgDa5NGx. All three are testnet; none is a key.
SWAP_ID = "s_539d922e9ef0a5d8"
GRC_DEPOSIT = GRC_DESK_DEPOSIT
DESK_XRP = XRP_HOT_ACCOUNT
PAID_XRP = XRP_CUSTOMER_PAYOUT


class RecordingDaemon:
    """A daemon that answers the two reads and REMEMBERS WHAT IT WAS ASKED FOR.

    The method list is the point, not a convenience: this file's strongest claim
    is that a custody report makes no wallet WRITE, and `getnewaddress` is a
    write -- it derives and stores a key. Asserting on recorded method names is
    the behavioral form of that claim, and it is what tests/test_chain_balances.py
    already does for the balance reader.
    """

    def __init__(self, walletname: str = "", loaded=None, ismine: bool | None = True, fail: str = ""):
        self.walletname = walletname
        self.loaded = [walletname] if loaded is None else loaded
        self.ismine = ismine
        self.fail = fail
        self.calls: list[str] = []

    def call(self, method: str, *params):
        self.calls.append(method)
        if self.fail:
            raise RuntimeError(self.fail)
        if method == "getwalletinfo":
            return {"walletname": self.walletname, "walletversion": 169900}
        if method == "listwallets":
            return list(self.loaded)
        raise AssertionError(f"a custody report asked for {method!r}, which is not one of its two reads")

    def address_ownership(self, address: str) -> AddressOwnership:
        self.calls.append("address_ownership")
        return AddressOwnership(self.ismine, "validateaddress answered ismine")


def swap_row(**overrides) -> dict:
    row = {
        "id": SWAP_ID, "from_asset": "GRC", "to_asset": "XRP",
        "deposit_address": GRC_DEPOSIT, "deposit_tag": None, "payout_address": PAID_XRP,
        "status": "completed", "payout_txid": "simulated",
    }
    row.update(overrides)
    return row


# --- the vocabulary itself -----------------------------------------------------

def test_every_verdict_state_is_a_DIFFERENT_string():
    """A function returning a constant has to fail, and this is what makes it fail.

    Without this, every verdict test below could be satisfied by a module whose
    states were all the same word: "the sentence names the variable" and "the
    state is X" would both pass while the report said one thing in every case.
    That is not hypothetical paranoia about tests -- it is the shape of the
    defect this whole module is about, where one answer covered two questions.

    MUTATION (ran, caught): set NOT_SEPARATED = SEPARATED. This fails, and so do
    six of the verdict tests below, which is the right blast radius.
    """
    assert len(set(STATES)) == len(STATES), f"two states share a spelling: {STATES}"
    # The literal IS the assertion (pyproject turns PLR2004 off in tests for exactly
    # this): seven states are the closed set, and an eighth has to be added here
    # deliberately, with a decision about whether wallet_custody.GOOD_STATES
    # includes it -- which is the decision that determines an exit code.
    assert len(STATES) == 7, (
        "a state was added or removed; decide whether wallet_custody.GOOD_STATES should include it"
    )


def test_every_state_FITS_the_label_column_it_is_printed_in():
    """wallet_custody.py prints the state AS the label column. A longer one is cut.

    MEASURED ON THE TOOL'S OWN OUTPUT, 2026-10-03. BY_DESIGN was first spelled
    "ONE DESK ACCOUNT, BY DESIGN" and the tally rendered

        ONE DESK ACCOUN 0 of 6  (none)

    -- a word cut mid-syllable with nothing saying a tool did it, which is exactly
    the ambiguity report_block.clipped() exists to refuse. The fix was the state,
    not the column.

    MUTATION (ran, caught): restore BY_DESIGN = "ONE DESK ACCOUNT, BY DESIGN".
    This fails with the length in the message.
    """
    for state in STATES:
        assert len(state) < LABEL_WIDTH, (
            f"{state!r} is {len(state)} characters and the label column is {LABEL_WIDTH}, so it "
            f"renders with no gap before the value or gets cut"
        )


def test_the_limits_of_the_report_name_the_one_that_matters_most():
    """The ownership limit has to be stated, because it is the one a reader assumes away.

    MUTATION (ran, caught): drop the first sentence from
    what_this_cannot_establish(). This fails. MUTATION (ran, SURVIVED at first):
    return a single joined paragraph instead of a tuple of lines -- the substring
    assertions all still passed. The count assertion below is what catches it,
    and it is there because wallet_custody.py prints these through
    report_block.wrapped() one at a time; a single blob would render as one
    40-line paragraph nobody reads.
    """
    limits = what_this_cannot_establish()

    # Four is the number of distinct limits the module names, not a round figure.
    assert len(limits) >= 4, f"the report lost a limit; it states {len(limits)}"
    joined = " ".join(limits).lower()
    assert "whose money a coin is" in joined, "the ownership limit is the one nobody can establish"
    assert "isdesks" in joined, "name the field that does not exist, so a reader stops looking for it"
    assert "never a pass" in joined, "NOT ESTABLISHED must be disclaimed in the limits, not only in a verdict"


# --- BTC / LTC / GRC: which wallet does the endpoint serve ---------------------

def test_an_unset_wallet_variable_is_NOT_SEPARATED_and_names_the_variable():
    """The state the operator's host was in, and the sentence that explains it.

    MUTATION (ran, caught): return SEPARATED when `configured_wallet` is empty.
    This fails.

    MUTATION (ran, SURVIVED, now caught): delete the closing remedy clause,
    `Set <ASSET>_RPC_WALLET to a wallet created for the desk`. The bare
    "GRC_RPC_WALLET" assertion still passed, because the variable is ALSO named in
    the sentence's opening clause ("GRC_RPC_WALLET is unset") -- so the test was
    measuring that the variable appears somewhere, which a verdict that diagnosed
    the problem and prescribed nothing satisfies. The remedy is the only part an
    operator acts on, so it is asserted as the whole phrase, not as a token.
    """
    verdict = script_chain_verdict("GRC", "", {"walletname": ""}, "", ())

    assert verdict.state == NOT_SEPARATED
    assert "GRC_RPC_WALLET is unset" in verdict.why, "name the variable AND say what is wrong with it"
    assert "Set GRC_RPC_WALLET to a wallet" in verdict.why, (
        "the remedy clause is the only part the operator acts on; naming the variable in the "
        "diagnosis is not the same as prescribing the fix"
    )
    assert "self-transfer" in verdict.why, "say what the shared wallet COSTS, not only that it is shared"


def test_a_DEFAULT_wallet_with_a_NAME_is_still_not_separated():
    """The case a config-text check would have got wrong, and the reason it is behavioral.

    A daemon can serve a NAMED wallet as its default -- one wallet loaded, and it
    has a name. With no /wallet/<name> in the URL this process reaches it and so
    does a bare CLI call, so the name changes nothing about custody. A check that
    read `walletname` and concluded "named, therefore separate" would report this
    as separated.

    MUTATION (ran, caught): branch on `serving` being non-empty before branching on
    `asked_for`. This fails: the verdict becomes SEPARATED for a wallet nothing
    asked for.
    """
    verdict = script_chain_verdict("BTC", "", {"walletname": "operator_main"}, "", ("operator_main",))

    assert verdict.state == NOT_SEPARATED
    assert "operator_main" in verdict.why, "name the wallet it is sharing, so the reader can go look at it"


def test_a_named_wallet_with_ANOTHER_loaded_is_the_strongest_answer_available():
    """Two wallets loaded means the DAEMON refuses a bare wallet RPC. rpc code -19.

    This is the one positive verdict that rests on something other than
    configuration: with more than one wallet loaded, Bitcoin Core answers a bare
    wallet call with "Wallet file not specified", so an operator's own
    `getnewaddress` cannot silently land in the desk's wallet.

    MUTATION (ran, caught): ignore `loaded_wallets` and always print the weaker
    clause. This fails on the -19 assertion. MUTATION (ran, caught): drop the
    "WHAT THIS DOES NOT ESTABLISH" clause -- the last assertion fails, which is
    the clause that stops a SEPARATED line being read as an audit of the funding.
    """
    verdict = script_chain_verdict("GRC", "desk_hot", {"walletname": "desk_hot"}, "", ("", "desk_hot"))

    assert verdict.state == SEPARATED
    assert "-19" in verdict.why, "the daemon's own refusal is the evidence; name its code"
    assert "/wallet/desk_hot" in verdict.why, "say how this process reaches it"
    assert "does not establish" in verdict.why.lower(), (
        "a SEPARATED verdict must carry its limit, or it is read as proof the coins were never the "
        "operator's -- which nothing can establish"
    )


def test_a_named_wallet_that_is_the_ONLY_one_loaded_says_it_is_weaker():
    """Same name, materially weaker state, and the two must not render alike.

    One loaded wallet means a bare wallet RPC still routes here, so the operator's
    own CLI reaches the desk's wallet. The verdict is still SEPARATED -- this
    process does address it explicitly -- and the sentence has to say what is
    missing, because "SEPARATED" alone would be the same word as the case above.

    MUTATION (ran, caught): return the `others` branch's sentence unconditionally.
    This fails on "WEAKER".
    """
    verdict = script_chain_verdict("LTC", "desk_hot", {"walletname": "desk_hot"}, "", ("desk_hot",))

    assert verdict.state == SEPARATED
    assert "WEAKER" in verdict.why
    assert "-19" not in verdict.why, "do not claim the daemon enforces anything when only one wallet is loaded"


def test_listwallets_not_read_is_reported_as_not_read_rather_than_as_either_answer():
    """None is not an empty list, and the difference is a claim about a daemon.

    `listwallets` is absent on older builds, Gridcoin among them, and its absence
    costs exactly one clause. Rendering it as the strong clause would invent the
    daemon's -19 refusal; rendering it as the weak one would assert this is the
    only loaded wallet, which was never read.

    MUTATION (ran, caught): treat None as () so it falls into the WEAKER branch.
    This fails: the sentence then claims listwallets reported something.
    """
    verdict = script_chain_verdict("GRC", "desk_hot", {"walletname": "desk_hot"}, "", None)

    assert verdict.state == SEPARATED
    assert "listwallets was not read" in verdict.why
    assert "ONLY loaded wallet" not in verdict.why
    assert "-19" not in verdict.why


def test_the_daemon_serving_a_DIFFERENT_wallet_is_MISCONFIGURED_and_names_both():
    """What this process asked for and what the daemon says are two names for one wallet.

    MUTATION (ran, caught): compare nothing and return SEPARATED whenever
    `asked_for` is set. This fails -- which is the point of asking the daemon at
    all rather than reading the config value, since the config value is what the
    mutation would have been trusting.
    """
    verdict = script_chain_verdict("BTC", "desk_hot", {"walletname": "operator_main"}, "", ("operator_main",))

    assert verdict.state == MISCONFIGURED
    assert "desk_hot" in verdict.why and "operator_main" in verdict.why, "both names, or the reader cannot act"
    assert "NOT established" in verdict.why


@pytest.mark.parametrize(
    ("walletinfo", "read_error", "marker"),
    [
        (None, "RPCError: No wallet is loaded. (rpc code -18)", "rpc code -18"),
        (None, "ConnectionError: [Errno 111] Connection refused", "Connection refused"),
        (["not", "a", "dict"], "", "list rather than an object"),
        ({"walletversion": 169900}, "", "no `walletname` field"),
    ],
)
def test_nothing_a_daemon_can_fail_to_say_produces_a_green_verdict(walletinfo, read_error, marker):
    """FOUR ways to not get an answer, and every one of them is NOT ESTABLISHED.

    This is the assertion the whole tool rests on. A down daemon, a refused login,
    an unloaded wallet, a reply of the wrong shape and an older build without
    `walletname` must never render as either answer -- a custody report whose
    default is green reports a separation it never measured.

    MUTATION (ran, caught): make the `read_error` branch return SEPARATED. This
    fails on two of the four parameters. MUTATION (ran, caught): delete the
    `isinstance(walletinfo, dict)` guard -- the list case then raises TypeError
    inside the function, which this test catches as an error rather than a verdict.
    """
    verdict = script_chain_verdict("GRC", "desk_hot", walletinfo, read_error, ("desk_hot",))

    assert verdict.state == NOT_ESTABLISHED
    assert marker in verdict.why, f"the reason has to survive into the sentence: {verdict.why}"
    assert verdict.why, "a NOT ESTABLISHED verdict with no reason is the thing it exists to replace"


def test_every_verdict_this_module_can_return_carries_a_reason():
    """`why` is never empty, including for the good states. That is the whole shape.

    A bare state is a boolean with extra steps. The reason is what carries the
    limit on a SEPARATED verdict and the remedy on a NOT_SEPARATED one, and a
    caller printing a state with no sentence has nothing for the operator to act
    on (rule 14: say what the number means next to the number).

    MUTATION (ran, caught): return CustodyVerdict(SEPARATED, "") from the
    named-wallet branch. This fails.
    """
    verdicts = [
        script_chain_verdict("GRC", "", {"walletname": ""}, "", ()),
        script_chain_verdict("GRC", "desk_hot", {"walletname": "desk_hot"}, "", ("", "desk_hot")),
        script_chain_verdict("GRC", "desk_hot", {"walletname": "other"}, "", ()),
        script_chain_verdict("GRC", "desk_hot", None, "boom", None),
        deposit_address_verdict("GRC", SWAP_ID, GRC_DEPOSIT, True),
        deposit_address_verdict("GRC", SWAP_ID, GRC_DEPOSIT, False),
        deposit_address_verdict("GRC", SWAP_ID, GRC_DEPOSIT, None, "validateaddress: refused"),
        xrp_desk_account_verdict(DESK_XRP, DESK_XRP),
        xrp_desk_account_verdict(DESK_XRP, PAID_XRP),
        xrp_desk_account_verdict("", ""),
        xrp_payout_destination_verdict(DESK_XRP, SWAP_ID, DESK_XRP),
        xrp_payout_destination_verdict(DESK_XRP, SWAP_ID, PAID_XRP),
        solana_account_verdict("", ""),
        solana_account_verdict("depositacct", "hotwallet"),
        solana_account_verdict("oneacct", "oneacct"),
    ]
    for verdict in verdicts:
        assert verdict.state in STATES, f"{verdict.state!r} is not in the closed set"
        # 40 characters is "a sentence rather than a word". The exact figure is not
        # the claim; that every verdict carries one is.
        assert len(verdict.why) > 40, f"a verdict with a stub reason: {verdict}"


# --- the per-swap deposit address ---------------------------------------------

def test_the_desk_owning_its_own_deposit_address_is_CORRECT_and_not_a_finding():
    """ismine=true is the custodial design. Reporting it as a defect was the first draft.

    The desk derives the deposit address with getnewaddress in the wallet it pays
    out of, so it owns it. The first version of this module rendered that as NOT
    SEPARATED, which put a correct answer in the same bucket as the defect and
    made wallet_custody.py exit non-zero for being right.

    MUTATION (ran, caught): return NOT_SEPARATED for owns=True. This fails here
    AND in test_a_report_of_only_good_states_exits_zero, because DESK_OWNS is in
    GOOD_STATES and NOT_SEPARATED is not -- which is the coupling that matters.
    """
    verdict = deposit_address_verdict("GRC", SWAP_ID, GRC_DEPOSIT, True)

    assert verdict.state == DESK_OWNS
    assert verdict.state in wallet_custody.GOOD_STATES, "a correct answer must not make the tool exit non-zero"
    assert "CORRECT for a custodial desk" in verdict.why
    assert "self-transfer" in verdict.why, (
        "the line has to say what ismine=true does NOT establish, which is that the deposit moved custody"
    )


def test_a_deposit_address_the_payout_wallet_does_not_hold_is_the_defect():
    """ismine=false means the desk cannot spend the money it was paid.

    MUTATION (ran, caught): return DESK_OWNS for owns=False. This fails, and so
    does the exit-code test, because NOT_THE_DESKS is deliberately outside
    GOOD_STATES.
    """
    verdict = deposit_address_verdict("GRC", SWAP_ID, GRC_DEPOSIT, False)

    assert verdict.state == NOT_THE_DESKS
    assert verdict.state not in wallet_custody.GOOD_STATES
    assert "DEFECT" in verdict.why
    assert "GRC_RPC_WALLET changed" in verdict.why, "name the commonest cause, since it is the one to check"


def test_an_unanswered_ownership_question_is_not_rendered_as_not_ours():
    """None is "nobody answered", and chains/base.py has the measurement for why.

    On 2026-10-01 a diagnostic asked this from a shell with no GRC_RPC_* exported,
    both calls died on ECONNREFUSED, owns_address() returned None, and the None
    was read as "this daemon has no `ismine` field" and reported to the operator
    as a fact. It was a transport failure.

    MUTATION (ran, caught): return NOT_THE_DESKS for owns=None. This fails on the
    state and on the "nobody answered" assertion.
    """
    verdict = deposit_address_verdict("GRC", SWAP_ID, GRC_DEPOSIT, None, "validateaddress: Connection refused")

    assert verdict.state == NOT_ESTABLISHED
    assert "nobody answered" in verdict.why
    assert "Connection refused" in verdict.why, "the collected reason has to reach the sentence"


# --- XRP ----------------------------------------------------------------------

def test_one_XRP_desk_account_serving_both_directions_is_BY_DESIGN():
    """Not a defect, and a tool that called it one would disagree with the design.

    services/swap_service.payout_source_account() reads the SAME variable through
    the same table as deposit_account(): an XRP deposit lands in
    XRP_DEPOSIT_ACCOUNT and an XRP payout is debited from it, exactly as one
    Gridcoin wallet takes deposits in and pays out of one balance.

    MUTATION (ran, caught): return NOT_SEPARATED when the two agree. This fails
    here and in the exit-code test, because BY_DESIGN is in GOOD_STATES.
    """
    verdict = xrp_desk_account_verdict(DESK_XRP, DESK_XRP)

    assert verdict.state == BY_DESIGN
    assert verdict.state in wallet_custody.GOOD_STATES
    assert "NOT a defect" in verdict.why
    assert "payout_source_account" in verdict.why, "point at the authority, so the claim is checkable"


def test_a_deposit_account_the_seed_does_not_control_is_MISCONFIGURED():
    """The case that costs money is "set to something else", not "unset".

    chains/xrp_signing.derive_and_check() refuses a seed paired with an account it
    does not control -- but it refuses at PAYOUT time, after the customer's
    deposit is confirmed and irreversible.

    MUTATION (ran, caught): compare nothing and always return BY_DESIGN. This
    fails. MUTATION (ran, caught): drop the "after the customer's deposit is
    confirmed" clause -- the last assertion fails, and that clause is the reason
    this is checked before a swap rather than at the send.
    """
    verdict = xrp_desk_account_verdict(DESK_XRP, PAID_XRP)

    assert verdict.state == MISCONFIGURED
    assert DESK_XRP in verdict.why and PAID_XRP in verdict.why, "both accounts, or the reader cannot act"
    assert "irreversible" in verdict.why


@pytest.mark.parametrize(
    ("configured", "derived", "error", "marker"),
    [
        ("", "", "", "XRP_DEPOSIT_ACCOUNT is unset"),
        (DESK_XRP, "", "", "no account was derived"),
        (DESK_XRP, "", "XRP_PAYOUT_SECRET_SEED is not set in this process", "not set in this process"),
    ],
)
def test_an_unverifiable_XRP_pairing_is_NOT_ESTABLISHED_rather_than_either_answer(
    configured, derived, error, marker
):
    """Three ways the pairing cannot be checked, none of which is an answer.

    MUTATION (ran, caught): return BY_DESIGN when `derived` is empty, on the
    reasoning that an unset seed means nothing to disagree with. This fails on all
    three parameters, and it is exactly the reassuring-answer error rule 17 names.
    """
    verdict = xrp_desk_account_verdict(configured, derived, error)

    assert verdict.state == NOT_ESTABLISHED
    assert marker in verdict.why


def test_a_payout_to_the_desks_own_account_moved_no_custody():
    """The check swap s_539d922e9ef0a5d8 needed, and the one no surface had.

    A Payment whose destination is the desk's own account is validated, pays a
    fee, and leaves the balance where it started. Every surface in this tree
    renders it as paid.

    MUTATION (ran, caught): return SEPARATED when the two accounts are equal.
    This fails here and in the exit-code test.
    """
    verdict = xrp_payout_destination_verdict(DESK_XRP, SWAP_ID, DESK_XRP)

    assert verdict.state == NOT_SEPARATED
    assert "moved no custody" in verdict.why
    assert "tesSUCCESS" in verdict.why, "say that the ledger reports success for it, since that is the trap"


def test_a_payout_that_left_the_desk_does_NOT_claim_the_destination_is_the_customers():
    """The limit is absolute rather than a gap to be filled later.

    The XRP Ledger has no owner field, chains/xrp.XRPAdapter.owns_address()
    returns None by design for that reason, and a desk cannot tell its operator's
    second faucet account from a stranger's. The 2026-10-03 payout went between
    two of the operator's OWN accounts and this verdict is the one it would get.

    MUTATION (ran, caught): drop the "WHAT THIS DOES NOT ESTABLISH" clause. This
    fails -- and the verdict would then read as proof the customer was paid, which
    is the overclaim the whole module is shaped to avoid.
    """
    verdict = xrp_payout_destination_verdict(DESK_XRP, SWAP_ID, PAID_XRP)

    assert verdict.state == SEPARATED
    assert "left the desk" in verdict.why
    assert "DOES NOT ESTABLISH" in verdict.why
    assert "customer" in verdict.why.lower()


def test_an_empty_account_on_either_side_is_NOT_ESTABLISHED_not_a_comparison():
    """"" == "" is True in Python and is not an answer about an account.

    MUTATION (ran, caught): delete the empty-string guard. Both empties then
    compare equal and the verdict becomes NOT_SEPARATED -- "the payout address IS
    the desk's own account", about two accounts that do not exist.
    """
    assert xrp_payout_destination_verdict("", SWAP_ID, "").state == NOT_ESTABLISHED
    assert xrp_payout_destination_verdict(DESK_XRP, SWAP_ID, "").state == NOT_ESTABLISHED
    assert xrp_payout_destination_verdict("", SWAP_ID, PAID_XRP).state == NOT_ESTABLISHED


# --- SOL ----------------------------------------------------------------------

@pytest.mark.parametrize(
    ("deposit", "hot", "state", "marker"),
    [
        ("", "", NOT_ESTABLISHED, "neither SOL_DEPOSIT_ACCOUNT nor SOL_HOT_WALLET"),
        ("depositacct", "", NOT_ESTABLISHED, "SOL_HOT_WALLET is unset"),
        ("", "hotwallet", NOT_ESTABLISHED, "SOL_DEPOSIT_ACCOUNT is unset"),
        ("oneacct", "oneacct", NOT_SEPARATED, "SAME account"),
        ("depositacct", "hotwallet", SEPARATED, "two different"),
    ],
)
def test_the_two_Solana_variables_get_five_distinct_answers(deposit, hot, state, marker):
    """Solana has TWO variables by design, so one account is a choice and not the shape.

    config.py carries the custody difference: "an XRP account is funded past a
    base reserve and holds nothing else; a Solana deposit account is an ordinary
    keypair's public key, and whoever holds that key holds every deposit between
    arrival and payout."

    MUTATION (ran, caught): return SEPARATED whenever both are non-empty, dropping
    the equality test. The ("oneacct", "oneacct") parameter fails. MUTATION (ran,
    caught): collapse the two half-set branches into one sentence -- the two
    middle parameters fail on the marker, and an operator would be told to check
    the variable that was already set.
    """
    verdict = solana_account_verdict(deposit, hot)

    assert verdict.state == state
    assert marker in verdict.why


def test_the_Solana_verdict_does_not_claim_to_know_whose_keypairs_those_are():
    """Two public keys compared as strings establish two accounts, and nothing else.

    MUTATION (ran, caught): drop the "WHAT THIS DOES NOT ESTABLISH" clause from
    the SEPARATED sentence. This fails.
    """
    verdict = solana_account_verdict("depositacct", "hotwallet")

    assert "DOES NOT ESTABLISH" in verdict.why
    assert "never opens a keypair file" in verdict.why


# --- the shared wallet label ---------------------------------------------------

def test_the_shared_wallet_label_says_what_an_unset_variable_MEANS():
    """It said "(default wallet)" in two files, which is true and tells nobody anything.

    MEASURED 2026-10-03: workers/common.endpoint_lines() and
    services/admin_view._endpoint_text() each spelled that phrase for the same
    question (rule 8's two copies), and it is the only place on a worker banner or
    the admin page's Chains table where the custody question is visible.

    MUTATION (ran, caught): return "(default wallet)" for an empty value. This
    fails: the variable name is what the operator needs and the phrase does not
    carry it.
    """
    assert wallet_label("GRC", "") == (
        "(default -- GRC_RPC_WALLET unset, so this is the wallet a bare CLI call reaches)"
    )
    assert wallet_label("GRC", "desk_hot") == "desk_hot"
    assert wallet_label("GRC", "  desk_hot  ") == "desk_hot", "a shell export with spaces is the same wallet"


def test_both_banner_surfaces_read_the_shared_label_rather_than_their_own_copy():
    """The merge, asserted on the SOURCE because the two renderings are far apart.

    This is the one structural assertion in the file, and it is here because the
    thing it guards cannot be reached from one rendered page: the worker banner
    and the admin table are produced by different processes. What it pins is that
    neither file re-grew the literal.

    READ AS AN AST AND NOT AS TEXT, AND THE FIRST VERSION OF THIS TEST FAILED ON
    ITS OWN SUBJECT. A substring search for '"(default wallet)"' matched the
    COMMENT this change left at both sites, which quotes the old phrase in order
    to say why it was replaced -- so the test failed on the very explanation rule 1
    asks for. Comments are not in an AST, so collecting string CONSTANTS asks the
    question that was meant: does this module still produce that phrase. It is the
    same reasoning as the wallet-write test below, which names getnewaddress in
    prose precisely so it can say it is never called.

    MUTATION (ran, caught): put `or "(default wallet)"` back into
    services/admin_view._endpoint_text(). This fails on the literal assertion.
    """
    for relative in ("swap_terminal/workers/common.py", "swap_terminal/services/admin_view.py"):
        source = (REPO_ROOT / relative).read_text()
        assert "wallet_label(" in source, f"{relative} no longer reads the shared label"
        literals = {
            node.value for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        assert "(default wallet)" not in literals, (
            f"{relative} spells the fallback phrase again as a string it can print; there is one, in "
            f"services/custody_separation.wallet_label()"
        )


# --- the entry point: no writes, no socket on a mainnet port, honest exit code --

#: Every RPC method that changes a wallet or moves money. A custody report must
#: name none of them, and `getnewaddress` is the one worth stating out loud: it
#: derives and stores a key, so it is a WRITE, and it is the method the deposit
#: path legitimately uses one layer away from here.
WALLET_WRITING_METHODS = (
    "getnewaddress", "sendtoaddress", "sendmany", "sendrawtransaction", "walletpassphrase",
    "dumpprivkey", "dumpwallet", "importprivkey", "createwallet", "loadwallet", "unloadwallet",
    "settxfee", "signrawtransaction", "signrawtransactionwithwallet",
)


def test_the_custody_report_names_no_wallet_WRITE_anywhere_in_its_source():
    """Walked as an AST, the way tests/test_chain_balances.py does for the balance reader.

    A string search would match the prose in the docstrings, which deliberately
    NAMES getnewaddress in order to say it is not called -- so this reads the
    attribute and call names the module actually contains, and prose cannot
    satisfy or break it.

    MUTATION (ran, caught): add `adapter.call("getnewaddress", "x")` inside
    read_script_chain(). This fails and names the method.
    """
    tree = ast.parse((REPO_ROOT / "wallet_custody.py").read_text())
    literals = {
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        # A docstring or a long sentence is prose; an RPC method name is a bare token.
        and " " not in node.value and "\n" not in node.value
    }
    names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    for method in WALLET_WRITING_METHODS:
        assert method not in literals, f"wallet_custody.py passes {method!r} as an RPC method name"
        assert method not in names, f"wallet_custody.py calls .{method}()"


def test_reading_a_script_chain_makes_exactly_the_two_reads_it_declares():
    """Behavioral, not a source scan: the stub refuses any method but those two.

    MUTATION (ran, caught): add a `getbalance` call to read_script_chain(). The
    stub raises AssertionError naming it, which this test surfaces.
    """
    daemon = RecordingDaemon(walletname="desk_hot", loaded=["", "desk_hot"])

    info, error, loaded = wallet_custody.read_script_chain("GRC", daemon)

    assert daemon.calls == ["getwalletinfo", "listwallets"]
    assert info == {"walletname": "desk_hot", "walletversion": 169900}
    assert error == ""
    assert loaded == ("", "desk_hot")


def test_listwallets_failing_costs_one_clause_and_not_the_chains_verdict():
    """getwalletinfo answered; the second read is optional and its absence is reported.

    MUTATION (ran, caught): let the listwallets failure return
    (None, str(error), None) as though the chain had not answered at all. The
    verdict becomes NOT ESTABLISHED for a daemon that answered perfectly well.
    """
    class OnlyWalletInfo(RecordingDaemon):
        def call(self, method, *params):
            self.calls.append(method)
            if method == "listwallets":
                raise RuntimeError("Method not found (rpc code -32601)")
            return {"walletname": "desk_hot"}

    info, error, loaded = wallet_custody.read_script_chain("GRC", OnlyWalletInfo())

    assert info == {"walletname": "desk_hot"}
    assert error == ""
    assert loaded is None, "a failed listwallets must be None, not (), or the verdict claims it was read"
    assert script_chain_verdict("GRC", "desk_hot", info, error, loaded).state == SEPARATED


def test_a_mainnet_port_is_NOT_ESTABLISHED_and_no_socket_is_opened_at_all():
    """Looking is the hazard. 157,797 GRC reached a chat log this way on 2026-09-25.

    `getwalletinfo` carries a balance, so a custody report against a mainnet
    daemon prints the operator's real staking balance into whatever transcript the
    output lands in. The port is classified BEFORE a socket opens.

    ASSERTED AS "NO CALL WAS MADE", not merely as the verdict: a version that
    connected, read the wallet and then reported NOT ESTABLISHED would satisfy a
    verdict-only assertion while doing the exact thing this prevents.

    MUTATION (ran, caught): read the wallet first and classify afterwards. The
    recorded call list is non-empty and this fails.
    """
    daemon = RecordingDaemon()
    mainnet = CHAIN_PORTS["GRC"].mainnet_port
    rpc = dict(wallet_custody.Config.RPC["GRC"])
    rpc["port"] = mainnet
    original = wallet_custody.Config.RPC["GRC"]
    wallet_custody.Config.RPC["GRC"] = rpc
    try:
        lines = wallet_custody.script_chain_lines("GRC", {"GRC": daemon}, swap_row())
    finally:
        wallet_custody.Config.RPC["GRC"] = original

    assert daemon.calls == [], f"a socket was opened to a mainnet port: {daemon.calls}"
    assert [state for _name, state, _why in lines] == [NOT_ESTABLISHED]
    assert "NO SOCKET WAS OPENED" in lines[0][2]
    assert str(mainnet) in lines[0][2] and "MAINNET" in lines[0][2]


def test_the_moved_port_decision_refuses_every_case_but_a_test_chain():
    """network_target.may_read_a_wallet(), which swap_readiness.chain_precheck() now wraps.

    MOVED THERE 2026-10-03 so that wallet_custody.py could refuse through the same
    four branches rather than spelling a second copy of a refusal to read a real
    wallet (rule 10 forbids a module importing a root entry point, so the
    alternatives were an upward import or a duplicate). The sentences moved
    verbatim, which is why tests/test_swap_readiness.py's assertions on them did
    not change.

    MUTATION (ran, caught): return True for UNRECOGNIZED, on the reasoning that an
    unknown port is at least not the mainnet one. This fails -- and any daemon can
    be started on any -rpcport, so "not a known mainnet port" is not "safe to
    read".
    """
    assert may_read_a_wallet("GRC", CHAIN_PORTS["GRC"].mainnet_port)[0] is False
    assert may_read_a_wallet("GRC", UNCONFIGURED_PORT)[0] is False
    assert may_read_a_wallet("GRC", 34567)[0] is False
    assert may_read_a_wallet("GRC", 25715) == (
        True, f"port 25715 is a test chain (mainnet is {CHAIN_PORTS['GRC'].mainnet_port})"
    )
    # A chain with no port convention at all must not fall through to True.
    assert may_read_a_wallet("XRP", 51234)[0] is False


def test_a_report_of_only_good_states_exits_zero_and_one_NOT_ESTABLISHED_does_not():
    """The exit code, which is the half somebody will want to loosen.

    A tool whose exit code reads 0 when a daemon did not answer reports custody
    separation it never measured. Both directions are asserted, because a version
    that always exited 1 would satisfy the second assertion alone.

    MUTATION (ran, caught): add NOT_ESTABLISHED to GOOD_STATES. The second half
    fails. MUTATION (ran, caught): return 0 unconditionally from verdict_lines().
    The second half fails.
    """
    good = [
        ("GRC wallet", SEPARATED, "a named wallet, reached through /wallet/desk_hot"),
        ("GRC deposit addr", DESK_OWNS, "the desk derived this address in its own wallet"),
        ("XRP desk account", BY_DESIGN, "one desk account, both directions"),
    ]
    lines, code = wallet_custody.verdict_lines(good)
    assert code == 0
    assert "SEPARATED: all 3 checks answered" in "\n".join(lines)
    assert "does NOT establish" in "\n".join(lines), "even an all-green run prints its limit"

    for bad_state in (NOT_SEPARATED, NOT_THE_DESKS, MISCONFIGURED, NOT_ESTABLISHED):
        lines, code = wallet_custody.verdict_lines([*good, ("one more", bad_state, "a reason")])
        assert code == 1, f"{bad_state} did not make the exit code non-zero"
        assert "did NOT answer the way a custodial desk needs" in "\n".join(lines)


def test_the_tally_prints_a_row_for_every_state_with_its_denominator():
    """Rule 3 and rule 14: a count needs what it was counted out of, and `(none)` is a result.

    A state silently absent from the tally is indistinguishable from a state
    nobody checked.

    MUTATION (ran, caught): skip states with no hits. The `(none)` assertions
    fail, and five of the seven rows vanish.
    """
    results = [("GRC wallet", NOT_SEPARATED, "shared"), ("XRP desk account", BY_DESIGN, "one account")]

    text = "\n".join(wallet_custody.tally_lines(results))

    assert "checks printed  2  <- the denominator" in text
    for state in STATES:
        assert state in text, f"the tally has no row for {state}"
    assert text.count("(none)") == len(STATES) - 2, "every state with no hits has to say (none)"
    assert "1 of 2" in text


# --- the XRP seed derivation that was merged, not added ------------------------

def test_the_derived_account_reader_never_returns_the_SEED_in_its_refusal(monkeypatch):
    """The refusal names the variable and the exception TYPE. Never the value.

    chains/xrp_payout_seed.derived_payout_account() reads the environment and
    hands back a PUBLIC classic address, which is why wallet_custody.py can ask
    the custody question without a seed ever reaching it.

    MUTATION (ran, SURVIVED, now caught) AND A MEASUREMENT THAT REFUTED THE
    PREMISE. The mutation is `interpolate str(error) into the refusal`, and the
    first version of this test ran it against the REAL library and passed anyway.
    Probed against the installed xrpl-py, 2026-10-03, eight values:

        "NOT-A-SEED-Zq7-UNIQUE-DO-NOT-PRINT"   ValueError: "Invalid character 'O'"
        "sEdTM1uX8pu2do5XvTnutH6HsouMaZZ9"     ValueError: 'Invalid checksum'
        '"sEdTM1uX8pu2do5XvTnutH6HsouMaM2"'    ValueError: 'Invalid character \'"\''
        "a" * 64                               ValueError: 'Invalid checksum'

    Not one of them echoes the whole value; the worst leaks ONE offending
    character. So this repository's standing comment -- that xrpl-py "has echoed
    the offending seed in its own messages" -- is NOT reproducible at the version
    installed here, and it is left standing rather than deleted because it is a
    claim about a library this code does not pin: a version that does echo is
    exactly what the guard is for, and the probe above cannot establish that none
    does (rule 17 -- "I could not reproduce it" is not "it never happens").

    Which means the canary has to come from a RAISE THIS TEST CONTROLS rather
    than from the library's own formatting. The stub below raises an exception
    whose message IS the seed, which is the behavior the guard exists to survive,
    and it catches the mutation on any version.
    """
    canary = "NOT-A-SEED-Zq7-UNIQUE-DO-NOT-PRINT"
    monkeypatch.setenv(SIGNING_SEED_ENV_VAR, canary)

    # First, end to end against the real library: a value it cannot read yields
    # no account, and the refusal names the variable rather than the value.
    address, refusal = derived_payout_account()
    assert address == "", "a value xrpl-py cannot read must not produce an account"
    assert canary not in refusal, f"the seed reached the refusal: {refusal}"
    assert SIGNING_SEED_ENV_VAR in refusal
    assert "does NOT decode" in refusal or "not importable" in refusal

    # Then with a library that DOES put the seed in its message. The import is at
    # call time inside derived_payout_account(), so patching the attribute reaches
    # it -- which is also why that import is deferred.
    import xrpl.wallet  # noqa: PLC0415 -- the module under patch; importing it at test scope would make xrpl-py mandatory for collection

    class EchoingWallet:
        @staticmethod
        def from_seed(seed):
            raise ValueError(f"could not read seed {seed}")

    monkeypatch.setattr(xrpl.wallet, "Wallet", EchoingWallet)
    address, refusal = derived_payout_account()

    assert address == ""
    assert canary not in refusal, (
        f"str(error) is being interpolated into the refusal, so a library version that echoes the "
        f"seed publishes it through this function: {refusal}"
    )
    assert "ValueError" in refusal, "the exception TYPE is what the refusal is allowed to carry"


def test_the_decode_check_still_answers_both_ways_after_the_derivation_was_merged(monkeypatch):
    """signing_seed_decodes() now delegates, and it kept its own sentence.

    The derivation existed twice in that module -- this function called
    Wallet.from_seed() and threw the wallet away, and derived_payout_account()
    keeps its address. The merge removed a copy rather than adding one, and this
    pins that both of that function's answers survived it.

    MUTATION (ran, caught): return True whenever the variable is merely SET. This
    fails on the unset case and on the nine-character placeholder that was on the
    operator's host on 2026-10-03.
    """
    monkeypatch.delenv(SIGNING_SEED_ENV_VAR, raising=False)
    decodes, why = signing_seed_decodes()
    assert decodes is False
    assert "is not set in this process" in why

    monkeypatch.setenv(SIGNING_SEED_ENV_VAR, "sEd1234567")
    decodes, why = signing_seed_decodes()
    assert decodes is False, "a ten-character value is not a seed; a 29-character base58 one is"
    assert SIGNING_SEED_ENV_VAR in why
