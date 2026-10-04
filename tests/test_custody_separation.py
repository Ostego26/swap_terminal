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
from typing import ClassVar

import pytest
from chains.base import AddressOwnership
from chains.xrp_payout_seed import SIGNING_SEED_ENV_VAR, derived_payout_account, signing_seed_decodes
from gridcoin_credentials import (
    OPERATOR_CREDENTIAL_VARIABLE,
    OPERATOR_HOST_VARIABLE,
    OPERATOR_PORT_VARIABLE,
    OPERATOR_REQUIRED_VARIABLES,
    OPERATOR_USER_VARIABLE,
    operator_endpoint,
)
from network_target import CHAIN_PORTS, UNCONFIGURED_PORT, may_read_a_wallet
from report_block import LABEL_WIDTH
from services.custody_separation import (
    BY_DESIGN,
    CANNOT_BE_ASKED,
    DESK_OWNERSHIP_FROM_STATE,
    DESK_OWNS,
    MISCONFIGURED,
    NOT_ESTABLISHED,
    NOT_SEPARATED,
    NOT_THE_DESKS,
    SEPARATED,
    STATES,
    cross_daemon_ownership_verdict,
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
    # this): eight states are the closed set, and a ninth has to be added here
    # deliberately, with a decision about whether wallet_custody.GOOD_STATES
    # includes it -- which is the decision that determines an exit code.
    #
    # SEVEN UNTIL 2026-10-04, when CANNOT_BE_ASKED split out of NOT_ESTABLISHED.
    # The decision this comment demands was made: it is NOT in GOOD_STATES, so the
    # exit code stays non-zero for it -- a question Gridcoin cannot answer is not
    # a separation established.
    assert len(STATES) == 8, (
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
    ("walletinfo", "read_error", "marker", "expected"),
    [
        (None, "RPCError: No wallet is loaded. (rpc code -18)", "rpc code -18", NOT_ESTABLISHED),
        (None, "ConnectionError: [Errno 111] Connection refused", "Connection refused", NOT_ESTABLISHED),
        (["not", "a", "dict"], "", "list rather than an object", NOT_ESTABLISHED),
        # THE FOURTH IS A DIFFERENT ABSENCE AND NOW SAYS SO. It asserted
        # NOT_ESTABLISHED alongside the other three until 2026-10-04 -- correct
        # that it was not green, wrong that it was the same kind of not-green. A
        # down daemon is fixed by starting it; a daemon whose getwalletinfo has no
        # `walletname` field is fixed by nothing an operator can type, and the
        # report told them to change a variable for it.
        ({"walletversion": 169900}, "", "no `walletname` field", CANNOT_BE_ASKED),
    ],
)
def test_nothing_a_daemon_can_fail_to_say_produces_a_green_verdict(walletinfo, read_error, marker, expected):
    """FOUR ways to not get an answer, and NONE of them is ever a green verdict.

    This is the assertion the whole tool rests on. A down daemon, a refused login,
    an unloaded wallet, a reply of the wrong shape and an older build without
    `walletname` must never render as either answer -- a custody report whose
    default is green reports a separation it never measured.

    THE STRONGER INVARIANT, pinned since 2026-10-04: not merely "not green" but
    WHICH absence. Three of these four are retryable and the fourth is not, and
    asserting one shared state over all four is what let wallet_custody.py print
    "Each line names the variable to change" for a line that names none. So the
    test asserts both: never in GOOD_STATES, and the specific state -- the second
    half is what a single shared spelling would now fail.

    MUTATION (ran, caught): make the `read_error` branch return SEPARATED. This
    fails on two of the four parameters. MUTATION (ran, caught): delete the
    `isinstance(walletinfo, dict)` guard -- the list case then raises TypeError
    inside the function, which this test catches as an error rather than a verdict.
    MUTATION (ran, caught): return NOT_ESTABLISHED from the `walletname` branch
    again -- the fourth parameter fails on the state while still passing the
    not-green half, which is precisely the gap this parameter was added to close.
    """
    verdict = script_chain_verdict("GRC", "desk_hot", walletinfo, read_error, ("desk_hot",))

    assert verdict.state not in wallet_custody.GOOD_STATES, "no absence may exit zero"
    assert verdict.state == expected, (
        f"a {expected!r} absence and a {NOT_ESTABLISHED!r} one have opposite remedies, so one "
        f"spelling for both is what the report then mis-renders; got {verdict.state!r}"
    )
    assert marker in verdict.why, f"the reason has to survive into the sentence: {verdict.why}"
    assert verdict.why, "an absence verdict with no reason is the thing it exists to replace"


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


# --- GRC: the cross-daemon ownership check ------------------------------------
#
# WHY THIS SECTION EXISTS AND WHY IT IS GRC-ONLY, MEASURED 2026-10-03 ON THE
# OPERATOR'S GRIDCOIN v5.5.1.0 TESTNET DAEMON (their measurement, pasted back; no
# network path from this test environment to it, so it is cited and not re-run --
# rule 17, say which you have):
#
#     gridcoinresearchd -testnet help | grep -iE '^(createwallet|loadwallet|listwallets|unloadwallet)'
#       -> (none of the multiwallet RPCs exist on this build)
#
# So Gridcoin has ONE wallet per datadir: no -rpcwallet, no /wallet/<name>
# endpoint, no `walletname` field. After the operator separated BTC and LTC into a
# `desk_hot` wallet, six of wallet_custody.py's seven checks answered and this was
# the one that could not:
#
#     NOT ESTABLISHED  GRC wallet: getwalletinfo answered with no `walletname`
#                      field, so which wallet this endpoint serves was not
#                      established.
#
# and no configuration could have changed that. The separation is instead PROVEN
# from the other direction: ask the daemon holding the OPERATOR's coins whether the
# DESK's deposit address is `ismine`. Their daemon on that day: datadir
# /home/mpjones26/.GridcoinResearch, rpcport 25715, in_sync true, balance
# 3780.08554497. The desk address is swap s_539d922e9ef0a5d8's own deposit_address,
# moaSBv8gcwXRnmQhxJJAjUvXMd542jsNNz, read from swap_terminal.db -- never from
# getnewaddress, which is a wallet WRITE.
#
# EVERY TEST BELOW WAS MUTATION-CHECKED and names its mutation and what it caught.

#: A second endpoint for the operator's own daemon, as the tests export it. 25715
#: is a REAL test port -- network_target.CHAIN_PORTS["GRC"].test_ports is
#: {25715, 25779, 9876} -- which is what lets the happy-path tests get past
#: may_read_a_wallet() without a daemon existing anywhere.
#:
#: THE PASSWORD IS A CANARY RATHER THAN A PLACEHOLDER. It is a value no report,
#: refusal or header line may ever contain, and three tests below assert its
#: absence. "secret" or "x" would have been satisfiable by accident.
#:
#: CREDENTIAL AND NOT PASS IN THE NAME, for the same reason
#: gridcoin_credentials.OPERATOR_CREDENTIAL_VARIABLE is spelled that way: ruff
#: S105 reads a ...PASS... constant assigned a string literal as a hardcoded
#: credential, which is exactly the shape it should flag. The forbidden move
#: (rule 19) would have been a `noqa` asserting the checker is wrong; this value
#: is a canary a test proves is NOT printed, so the name now says so.
OPERATOR_CREDENTIAL_CANARY = "NOT-A-PASSWORD-Qx9-UNIQUE-DO-NOT-PRINT"


def export_operator_endpoint(monkeypatch, port: str = "25715", host: str = "") -> None:
    """Export the four GRC_OPERATOR_RPC_* variables into one test's environment.

    monkeypatch, so the export is undone at teardown: these name an endpoint and
    carry a credential, and a test that leaked them into the process would change
    what every later test in the session resolves.
    """
    monkeypatch.setenv(OPERATOR_PORT_VARIABLE, port)
    monkeypatch.setenv(OPERATOR_USER_VARIABLE, "operatorrpc")
    monkeypatch.setenv(OPERATOR_CREDENTIAL_VARIABLE, OPERATOR_CREDENTIAL_CANARY)
    if host:
        monkeypatch.setenv(OPERATOR_HOST_VARIABLE, host)
    else:
        monkeypatch.delenv(OPERATOR_HOST_VARIABLE, raising=False)


class RecordingGridcoinAdapter:
    """Stands in for chains/gridcoin.GridcoinAdapter and RECORDS THAT IT WAS BUILT.

    Construction is recorded as well as calls, because the property several tests
    below protect is "no second socket was opened" -- and a version that built the
    adapter, connected, and then reported NOT ESTABLISHED would satisfy a
    verdict-only assertion while doing the exact thing the check refuses to do.

    It also keeps the kwargs it was handed, so one test can assert `wallet=""` --
    the operator's daemon has exactly one wallet and there is no /wallet/<name>
    path to ask for on this daemon family.
    """

    # ClassVar, so reset() can rebind them for each test and every instance the
    # production code constructs shares one record. Per-instance lists would hide
    # the thing these assert: that NO instance was built at all.
    built: ClassVar[list[dict]] = []
    asked: ClassVar[list[str]] = []
    answer: ClassVar[AddressOwnership] = AddressOwnership(False, "validateaddress answered ismine")

    def __init__(self, **kwargs):
        type(self).built.append(kwargs)

    def address_ownership(self, address: str) -> AddressOwnership:
        type(self).asked.append(address)
        return type(self).answer

    @classmethod
    def reset(cls, answer: AddressOwnership) -> None:
        cls.built = []
        cls.asked = []
        cls.answer = answer


def test_an_unset_second_endpoint_REFUSES_and_names_only_variable_NAMES(monkeypatch):
    """The refusal an operator sees first, and the secret it must not contain.

    CREDENTIALS ARE THE HARD PART OF THIS CHECK. The password is read from the
    ENVIRONMENT and from nowhere else -- never gridcoinresearch.conf, never a
    .env -- and this repository has already published a live
    GRIDCOIN_RPC_PASSWORD once, through a `.env.bak` that reached GitHub
    (CLAUDE.md rule 2). So the refusal names the VARIABLES that are unset and
    never what any of them is set to.

    MUTATION (ran, caught): return (None, "") with no sentence. The
    name-the-variables assertions fail, and the operator would be told the check
    did not run without being told what to export. (A FIRST ATTEMPT AT THIS
    MUTATION WAS A NO-OP and is recorded because a no-op scored as SURVIVED is
    indistinguishable from a weak test until somebody reads the diff: it prefixed
    the sentence rather than emptying it, so every assertion still had the whole
    sentence to match against.)

    MUTATION (ran, caught): resolve each operator variable with a fallback to the
    desk's own GRC_RPC_* name. operator_endpoint() then returns an endpoint and
    `endpoint is None` fails -- which is the whole point of there being no
    fallback: asking the DESK daemon whether it owns the desk's own address always
    answers yes, and reporting that as "one wallet serves both" is a
    guaranteed-wrong answer in the register of a measurement. (THE FIRST RUN OF
    THIS MUTATION SURVIVED, and the test was the reason: it set only two of the
    desk's three variables, so the mutant refused for the ordinary
    missing-variable reason and never exercised its fallback. All three are set
    below, which is the state the operator's host is actually in.)
    """
    for name in OPERATOR_REQUIRED_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    # ALL THREE of the desk's own variables, because a fallback mutation resolves
    # name-by-name and a partly set desk endpoint would refuse for the ordinary
    # reason instead -- which is how the first version of this test scored a
    # fallback mutation as CAUGHT when it had not been exercised at all. These are
    # the names config.py:516-519 reads, and on the operator's host all three are
    # set, which is exactly the state a fallback would succeed in.
    monkeypatch.setenv("GRC_RPC_PORT", "25715")
    monkeypatch.setenv("GRC_RPC_USER", "gridcoinrpc")
    monkeypatch.setenv("GRC_RPC_PASS", OPERATOR_CREDENTIAL_CANARY)

    endpoint, refusal = operator_endpoint()

    assert endpoint is None, (
        "an unset operator endpoint must REFUSE rather than fall back to the desk's own credentials"
    )
    for name in OPERATOR_REQUIRED_VARIABLES:
        assert name in refusal, f"the refusal has to name {name}; it is what the operator exports"
    assert OPERATOR_CREDENTIAL_CANARY not in refusal, f"a credential reached the refusal: {refusal}"
    assert "environment" in refusal.lower(), "say where the password is read from, since it is not a conf file"
    assert "gridcoinresearch.conf" in refusal, (
        "name the file it does NOT read: an operator whose conf holds the password will otherwise "
        "assume this found it there"
    )


def test_a_PARTLY_set_endpoint_refuses_and_names_only_the_missing_ones(monkeypatch):
    """Two of three exported is not configured, and the refusal says WHICH one is not.

    MUTATION (ran, caught): require only GRC_OPERATOR_RPC_PORT and default the
    other two. `endpoint is None` fails, and the tool would authenticate with an
    empty password and report the resulting 401 as NOT ESTABLISHED -- "the daemon
    did not answer" for a question it never asked properly.
    """
    monkeypatch.setenv(OPERATOR_PORT_VARIABLE, "25715")
    monkeypatch.setenv(OPERATOR_USER_VARIABLE, "operatorrpc")
    monkeypatch.delenv(OPERATOR_CREDENTIAL_VARIABLE, raising=False)

    endpoint, refusal = operator_endpoint()

    assert endpoint is None
    assert OPERATOR_CREDENTIAL_VARIABLE in refusal.split("Unset:")[1].split(".")[0], (
        "the missing list has to hold the one that is actually missing"
    )
    assert OPERATOR_PORT_VARIABLE not in refusal.split("Unset:")[1].split(".")[0], (
        "do not list a variable the operator already exported; they will go and re-export it"
    )


def test_an_EMPTY_export_counts_as_unset_rather_than_as_a_credential(monkeypatch):
    """`export GRC_OPERATOR_RPC_PASS=` is what a failed command substitution leaves.

    gridcoin_credentials._first_set() already carries the measurement: a rotation
    script wrote an empty password into a live .env that way on 2026-09-25.
    Treating it as configured authenticates with nothing and reports the daemon as
    broken.

    MUTATION (ran, caught): test `name in os.environ` instead of emptiness.
    `endpoint is None` fails on both parameters below.
    """
    monkeypatch.setenv(OPERATOR_PORT_VARIABLE, "25715")
    monkeypatch.setenv(OPERATOR_USER_VARIABLE, "operatorrpc")
    monkeypatch.setenv(OPERATOR_CREDENTIAL_VARIABLE, "")
    assert operator_endpoint()[0] is None, "an empty password is not a password"

    monkeypatch.setenv(OPERATOR_CREDENTIAL_VARIABLE, "   ")
    assert operator_endpoint()[0] is None, "a whitespace-only export is the same failure with spaces"


def test_a_non_integer_port_refuses_and_names_the_mainnet_port_it_is_not(monkeypatch):
    """A port that is not a number cannot be classified, so nothing is opened.

    MUTATION (ran, caught): `int(raw_port or 0)` with no try/except. A non-numeric
    value then raises ValueError out of a reporting tool instead of producing a
    line, and this test fails with that exception rather than a refusal.
    """
    export_operator_endpoint(monkeypatch, port="25715x")

    endpoint, refusal = operator_endpoint()

    assert endpoint is None
    assert OPERATOR_PORT_VARIABLE in refusal
    assert str(CHAIN_PORTS["GRC"].mainnet_port) in refusal, (
        "name the mainnet port as the one that will be refused, so a reader does not try it"
    )
    assert OPERATOR_CREDENTIAL_CANARY not in refusal


def test_the_endpoints_printable_label_is_host_and_port_and_carries_NO_credential(monkeypatch):
    """`label` is what every report line, header and refusal prints. It is host:port.

    MUTATION (ran, caught): include `self.user` and `self.password` in `label`, as
    a connection string would. The canary assertion fails -- and that string is
    pasted into chat logs by design, which is how 157,797 GRC of the operator's
    real staking balance reached one on 2026-09-25.
    """
    export_operator_endpoint(monkeypatch, host="127.0.0.1")
    endpoint, refusal = operator_endpoint()

    assert refusal == ""
    assert endpoint.label == "127.0.0.1:25715"
    assert OPERATOR_CREDENTIAL_CANARY not in endpoint.label
    assert endpoint.user not in endpoint.label, "not the user either; it is half a credential"
    assert endpoint.password == OPERATOR_CREDENTIAL_CANARY, "the value is still available to basic auth"


def test_an_unset_host_defaults_to_loopback_rather_than_refusing(monkeypatch):
    """HOST has a default and the other three do not, which is config.py's own split.

    config.py:518 defaults GRC_RPC_HOST to 127.0.0.1 for the same reason: a second
    DATADIR on this host is the separation this check is for, and a daemon on
    another machine is a different conversation.

    MUTATION (ran, caught): add OPERATOR_HOST_VARIABLE to
    OPERATOR_REQUIRED_VARIABLES. This refuses a correctly configured endpoint and
    the label assertion fails.
    """
    export_operator_endpoint(monkeypatch)
    endpoint, _refusal = operator_endpoint()

    assert endpoint is not None
    assert endpoint.label == "127.0.0.1:25715"


# --- the decision itself, with seeded answers ---------------------------------

def test_ismine_FALSE_from_the_operators_daemon_is_the_PROOF_of_separation():
    """The one positive verdict Gridcoin can produce, and what it refuses to claim.

    A `false` from the daemon holding the operator's coins is an observation about
    a KEY, not a reading of a config value -- which is why it is available on the
    pre-0.17 RPC surface Gridcoin actually has while `walletname` is not.

    MUTATION (ran, caught): return NOT_SEPARATED for operator_owns=False. This
    fails, and so does the exit-code coupling below, since SEPARATED is in
    GOOD_STATES and NOT_SEPARATED is not.

    MUTATION (ran, SURVIVED at first, now caught): drop the "no OTHER wallet of
    the operator's" clause and keep the generic "WHAT THIS DOES NOT ESTABLISH"
    sentence about funding. The first version of this test asserted only
    `"DOES NOT ESTABLISH" in why`, which the funding clause satisfies on its own --
    so a verdict that silently claimed the operator holds this key NOWHERE would
    have passed. One daemon and one datadir were asked; a second datadir, a
    restored backup and a watch-only import were not and could not be. The
    assertion is now the specific clause.
    """
    verdict = cross_daemon_ownership_verdict(
        "GRC", SWAP_ID, GRC_DEPOSIT, "127.0.0.1:25715", False, "validateaddress answered ismine", True
    )

    assert verdict.state == SEPARATED
    assert verdict.state in wallet_custody.GOOD_STATES
    assert "ismine=false" in verdict.why
    assert "127.0.0.1:25715" in verdict.why, "name the endpoint that answered, or the claim is uncheckable"
    assert "no OTHER wallet of the operator's holds this key" in verdict.why, (
        "ONE daemon and ONE datadir were asked. A verdict that does not say so reads as proof the "
        "operator holds this key nowhere, which is not available from one endpoint"
    )
    # THE REASON AND NOT ONLY THE CLAIM, and this pair of assertions is where a
    # mutation SURVIVED on the first pass. The mutation deleted "ONE daemon and ONE
    # datadir were asked, and a second datadir, a restored backup or a watch-only
    # import was not and could not be" while leaving the claim phrase above intact,
    # and the test passed -- so a verdict that stated the limit without saying WHY
    # it holds would have been scored as correct. An operator who is told "this does
    # not establish that no other wallet holds the key" and not told that exactly one
    # datadir was asked has no way to know what second thing to go and check.
    assert "ONE daemon and ONE datadir were asked" in verdict.why, (
        "the limit needs its reason: say that one endpoint and one datadir were asked"
    )
    assert "watch-only" in verdict.why, (
        "name the three things that were not asked -- a second datadir, a restored backup, a "
        "watch-only import -- because they are what an operator would otherwise have to guess at"
    )
    assert "funding is unaudited" in verdict.why, "the funding limit holds here as everywhere else"


def test_ismine_TRUE_from_BOTH_daemons_is_proof_they_are_the_SAME_wallet():
    """One key in two wallets is one wallet -- or a wallet.dat that was copied.

    THE COPY IS THE REASON THIS SENTENCE NAMES IT. Two independently created
    wallets do not share a key, so the only other way to reach this state is
    copying wallet.dat into the second datadir -- which copies the operator's keys
    and is the exact opposite of separating them. No other check in this tree could
    detect it, which is why docs/hot_wallet_separation_runbook.md now states it
    first and loudest.

    MUTATION (ran, caught): return SEPARATED when the operator's daemon answers
    true, on the reasoning that the two endpoints are two daemons. This fails here
    and in the exit-code test: a self-transfer would be reported as custody moving.
    """
    verdict = cross_daemon_ownership_verdict(
        "GRC", SWAP_ID, GRC_DEPOSIT, "127.0.0.1:25715", True, "validateaddress answered ismine", True
    )

    assert verdict.state == NOT_SEPARATED
    assert verdict.state not in wallet_custody.GOOD_STATES
    assert "BOTH daemons hold the key" in verdict.why
    assert "wallet.dat" in verdict.why, "name the mechanism that produces this state from two datadirs"
    assert "self-transfer" in verdict.why, "say what it COSTS, not only that the wallets are one"


def test_ismine_TRUE_from_the_operator_alone_is_still_NOT_SEPARATED_and_says_which_half():
    """The desk's half being unread does not soften the operator's answer.

    `ismine: true` from the daemon holding the operator's coins is already the
    finding: the key the desk derived is in the operator's wallet. What the desk
    answers cannot make that untrue, so the verdict does not wait for it -- and
    the sentence says which half was read, because "NOT SEPARATED" alone would be
    the same words as the two-answer case above.

    MUTATION (ran, caught): require desk_owns is True before returning
    NOT_SEPARATED, returning NOT_ESTABLISHED otherwise. This fails, and the state
    moves out of the exit-code's bad set for a daemon that reported the defect.
    """
    verdict = cross_daemon_ownership_verdict(
        "GRC", SWAP_ID, GRC_DEPOSIT, "127.0.0.1:25715", True, "validateaddress answered ismine", None
    )

    assert verdict.state == NOT_SEPARATED
    assert "not read" in verdict.why, "say that the desk's half was not read rather than implying it was"
    assert "BOTH daemons hold the key" not in verdict.why, (
        "do not claim the desk answered; only the operator's daemon did"
    )


def test_ismine_FALSE_from_BOTH_is_the_desk_being_unable_to_SPEND_its_own_deposit():
    """Separated from the operator, and still a defect -- the louder of the two.

    An address NEITHER wallet holds is a deposit nobody can spend. Folding it into
    SEPARATED would print a green line over an unspendable deposit, which is the
    shape of failure this whole module exists to refuse, so it gets the state the
    vocabulary already has for it.

    MUTATION (ran, caught): return SEPARATED whenever operator_owns is False,
    ignoring desk_owns. This fails, and the exit code would read 0 for a swap the
    desk cannot settle.
    """
    verdict = cross_daemon_ownership_verdict(
        "GRC", SWAP_ID, GRC_DEPOSIT, "127.0.0.1:25715", False, "validateaddress answered ismine", False
    )

    assert verdict.state == NOT_THE_DESKS
    assert verdict.state not in wallet_custody.GOOD_STATES
    assert "NEITHER daemon" in verdict.why
    assert "cannot spend" in verdict.why


def test_every_cross_daemon_state_is_a_DIFFERENT_one_so_a_constant_cannot_satisfy_it():
    """Five inputs, four distinct states. A constant-returning function fails here.

    This is the companion to test_every_verdict_state_is_a_DIFFERENT_string, one
    level down: that one pins the vocabulary, this one pins that THIS function
    actually ranges over it. Without it, each test above could be satisfied by a
    function that answered one way and carried a sentence mentioning every clause.

    MUTATION (ran, caught): return CustodyVerdict(SEPARATED, <the long sentence>)
    from every branch. This fails on the set size and names what collapsed.
    """
    states = [
        cross_daemon_ownership_verdict("GRC", SWAP_ID, "", "", None).state,
        cross_daemon_ownership_verdict("GRC", SWAP_ID, GRC_DEPOSIT, "h:1", None, "refused").state,
        cross_daemon_ownership_verdict("GRC", SWAP_ID, GRC_DEPOSIT, "h:1", True, "", True).state,
        cross_daemon_ownership_verdict("GRC", SWAP_ID, GRC_DEPOSIT, "h:1", False, "", True).state,
        cross_daemon_ownership_verdict("GRC", SWAP_ID, GRC_DEPOSIT, "h:1", False, "", False).state,
    ]

    assert states == [NOT_ESTABLISHED, NOT_ESTABLISHED, NOT_SEPARATED, SEPARATED, NOT_THE_DESKS], (
        f"the cross-daemon verdict no longer distinguishes its cases: {states}"
    )
    assert len(set(states)) == 4, f"four distinct answers collapsed to {len(set(states))}: {states}"
    for state in states:
        assert state in STATES, f"{state!r} is outside the closed set, so the tally has no row for it"


@pytest.mark.parametrize(
    ("operator_owns", "operator_why", "marker"),
    [
        (None, "validateaddress: Connection refused", "Connection refused"),
        (None, "validateaddress: answered, with no `ismine` field", "no `ismine` field"),
        (None, "", "no reason was returned"),
    ],
)
def test_an_unanswered_operator_daemon_is_NOT_ESTABLISHED_and_never_green(
    operator_owns, operator_why, marker
):
    """"Nobody answered" is not "not theirs", and chains/base.py has the measurement.

    On 2026-10-01 a diagnostic asked this from a shell with no GRC_RPC_* exported,
    both calls died on ECONNREFUSED, the None was read as "this daemon has no
    `ismine` field" and reported to the operator as a fact. It was a transport
    failure.

    MUTATION (ran, caught): return SEPARATED for operator_owns=None, on the
    reasoning that a daemon which cannot see the address at least did not claim it.
    All three parameters fail -- and that is the reassuring-answer error rule 17
    names, arriving as a green verdict and a zero exit code.
    """
    verdict = cross_daemon_ownership_verdict(
        "GRC", SWAP_ID, GRC_DEPOSIT, "127.0.0.1:25715", operator_owns, operator_why
    )

    assert verdict.state == NOT_ESTABLISHED
    assert verdict.state not in wallet_custody.GOOD_STATES
    assert marker in verdict.why
    assert "nobody answered" in verdict.why.lower()


def test_no_GRC_deposit_leg_says_so_and_does_not_default_to_either_answer():
    """The check REQUIRES --swap, and the reason it cannot invent an address.

    The desk address is the swap's own `deposit_address` from swap_terminal.db. The
    alternative -- `getnewaddress` -- would answer the same question and is a
    WALLET WRITE: it derives and stores a key. A read-only audit that paid for its
    answer with a new key would be spending the thing it audits.

    MUTATION (ran, caught): return SEPARATED when there is no address, on the
    reasoning that nothing was found in the operator's wallet. This fails.
    """
    verdict = cross_daemon_ownership_verdict("GRC", "", "", "", None)

    assert verdict.state == NOT_ESTABLISHED
    assert "--swap" in verdict.why
    assert "WALLET WRITE" in verdict.why, (
        "say why the address comes from the database, or somebody will 'fix' this with getnewaddress"
    )
    assert "no second socket was opened" in verdict.why


def test_the_desk_half_is_translated_from_the_state_rather_than_read_twice():
    """DESK_OWNERSHIP_FROM_STATE is the only mapping, and None is its honest default.

    wallet_custody.py has already asked the DESK's endpoint `ismine` for this
    address and rendered it through deposit_address_verdict(). The cross-daemon
    line translates that recorded state back instead of opening a second socket to
    the same daemon and asking the question again -- two reads of one fact are two
    chances for one report to disagree with itself (rule 8).

    MUTATION (ran, caught): add NOT_ESTABLISHED: False to the mapping, so an
    unanswered desk reads as "the desk does not own it". The None assertion fails,
    and the cross-daemon verdict would then report NOT THE DESK'S -- "neither
    daemon holds the key" -- for a desk daemon that was simply unreachable.
    """
    assert DESK_OWNERSHIP_FROM_STATE[DESK_OWNS] is True
    assert DESK_OWNERSHIP_FROM_STATE[NOT_THE_DESKS] is False
    for state in STATES:
        if state not in (DESK_OWNS, NOT_THE_DESKS):
            assert DESK_OWNERSHIP_FROM_STATE.get(state) is None, (
                f"{state!r} is not an `ismine` answer and must map to None, not to a bool"
            )
    # The two mapped states are exactly the two deposit_address_verdict() produces
    # from a bool, which is what makes the translation lossless rather than a guess.
    assert deposit_address_verdict("GRC", SWAP_ID, GRC_DEPOSIT, True).state == DESK_OWNS
    assert deposit_address_verdict("GRC", SWAP_ID, GRC_DEPOSIT, False).state == NOT_THE_DESKS


# --- the entry point: the port is classified before any second socket ---------

def test_the_cross_daemon_check_reads_the_DESK_state_under_the_name_it_is_printed_as():
    """Two spellings of one check name would silently lose the desk's answer forever.

    script_chain_lines() produces the deposit-address line as
    f"{asset} deposit addr" and cross_daemon_lines() looks it up by
    DESK_DEPOSIT_CHECK. If those drift, `next(...)` finds nothing, the desk's half
    reads as "not read" on every run, and NOTHING FAILS -- the report is merely
    permanently less informative, which is the invisible drift rule 8 is about.

    MUTATION (ran, caught): change DESK_DEPOSIT_CHECK to "GRC deposit address".
    This fails on the equality.
    """
    daemon = RecordingDaemon(walletname="", ismine=True)
    # 25715 because the port decides whether the deposit-address line is reached at
    # all: may_read_a_wallet() refuses the UNCONFIGURED default this test
    # environment has, and the wallet line is then the only line. The first version
    # of this test did not set it and failed on that, which is the port
    # classification working rather than an obstacle.
    rpc = dict(wallet_custody.Config.RPC["GRC"], port=25715)
    original = wallet_custody.Config.RPC["GRC"]
    wallet_custody.Config.RPC["GRC"] = rpc
    try:
        lines = wallet_custody.script_chain_lines("GRC", {"GRC": daemon}, swap_row())
    finally:
        wallet_custody.Config.RPC["GRC"] = original
    names = [name for name, _state, _why in lines]

    assert wallet_custody.DESK_DEPOSIT_CHECK in names, (
        f"cross_daemon_lines() looks up {wallet_custody.DESK_DEPOSIT_CHECK!r} and script_chain_lines() "
        f"printed {names}"
    )
    assert wallet_custody.CROSS_DAEMON_CHAIN == "GRC", (
        "BTC and LTC carry the multiwallet RPCs, so they answer from one endpoint and must not grow "
        "a second-daemon line (rule 8: two mechanisms for one question)"
    )


def test_a_MAINNET_operator_port_opens_NO_second_socket_at_all(monkeypatch):
    """Looking is the hazard, and the operator's MAINNET daemon is the one with coins.

    ASSERTED AS "NO ADAPTER WAS BUILT AND NO CALL WAS MADE", not merely as the
    verdict: a version that connected, asked, and then reported NOT ESTABLISHED
    would satisfy a verdict-only assertion while doing the exact thing this
    prevents. The classification runs through the SAME
    network_target.may_read_a_wallet() the desk's endpoint goes through, so there
    is one mainnet refusal in this tree rather than two.

    MUTATION (ran, caught): ask first and classify afterwards. `built` is non-empty
    and this fails. MUTATION (ran, caught): classify with a locally written
    `port != 15715` test instead of may_read_a_wallet(). The UNRECOGNIZED case in
    the next test then passes a socket through, which that test catches.
    """
    export_operator_endpoint(monkeypatch, port=str(CHAIN_PORTS["GRC"].mainnet_port))
    RecordingGridcoinAdapter.reset(AddressOwnership(False, "should never be reached"))
    monkeypatch.setattr(wallet_custody, "GridcoinAdapter", RecordingGridcoinAdapter)

    lines = wallet_custody.cross_daemon_lines(swap_row(), [])

    assert RecordingGridcoinAdapter.built == [], "an adapter was constructed against a MAINNET port"
    assert RecordingGridcoinAdapter.asked == [], "a socket was opened to a MAINNET port"
    assert [state for _name, state, _why in lines] == [NOT_ESTABLISHED]
    assert "NO SECOND SOCKET WAS OPENED" in lines[0][2]
    assert "MAINNET" in lines[0][2]
    assert OPERATOR_CREDENTIAL_CANARY not in lines[0][2], "a credential reached a printed report line"


def test_an_UNRECOGNIZED_operator_port_also_opens_no_socket(monkeypatch):
    """Any daemon can be started on any -rpcport, so "not the mainnet port" is not safe.

    MUTATION (ran, caught): accept any port that is not the mainnet one. `built`
    becomes non-empty for port 34567 and this fails -- and that port may be a
    mainnet daemon on a custom -rpcport, which is the reasoning
    may_read_a_wallet() already records.
    """
    export_operator_endpoint(monkeypatch, port="34567")
    RecordingGridcoinAdapter.reset(AddressOwnership(False, "should never be reached"))
    monkeypatch.setattr(wallet_custody, "GridcoinAdapter", RecordingGridcoinAdapter)

    lines = wallet_custody.cross_daemon_lines(swap_row(), [])

    assert RecordingGridcoinAdapter.built == []
    assert RecordingGridcoinAdapter.asked == []
    assert lines[0][1] == NOT_ESTABLISHED
    assert "34567" in lines[0][2]


def test_an_unconfigured_second_endpoint_opens_no_socket_and_names_what_to_export(monkeypatch):
    """No variables, no adapter, and a line that says the GRC question went unanswered.

    MUTATION (ran, caught): build the adapter before resolving the endpoint, with
    host/port defaults. `built` is non-empty and this fails -- and the tool would
    be connecting to a port nobody named.
    """
    for name in OPERATOR_REQUIRED_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    RecordingGridcoinAdapter.reset(AddressOwnership(False, "should never be reached"))
    monkeypatch.setattr(wallet_custody, "GridcoinAdapter", RecordingGridcoinAdapter)

    lines = wallet_custody.cross_daemon_lines(swap_row(), [])

    assert RecordingGridcoinAdapter.built == []
    assert lines[0][1] == NOT_ESTABLISHED
    for name in OPERATOR_REQUIRED_VARIABLES:
        assert name in lines[0][2]


def test_a_swap_with_no_GRC_deposit_leg_resolves_NO_variables_and_opens_no_socket(monkeypatch):
    """An XRP->GRC swap's deposit address is on XRP, so there is nothing to ask about.

    ASSERTED AS "NO SOCKET", because the cheap wrong version asks the operator's
    daemon whether it owns an XRP address: `validateaddress` answers isvalid=false
    with no `ismine`, which renders as NOT ESTABLISHED and looks identical to a
    daemon that is down.

    MUTATION (ran, caught): drop the from_asset test and pass whatever
    `deposit_address` holds. `asked` becomes non-empty, carrying the XRP address.
    """
    export_operator_endpoint(monkeypatch)
    RecordingGridcoinAdapter.reset(AddressOwnership(False, "should never be reached"))
    monkeypatch.setattr(wallet_custody, "GridcoinAdapter", RecordingGridcoinAdapter)

    lines = wallet_custody.cross_daemon_lines(
        swap_row(from_asset="XRP", to_asset="GRC", deposit_address=PAID_XRP), []
    )

    assert RecordingGridcoinAdapter.asked == [], "the operator's daemon was asked about an XRP address"
    assert RecordingGridcoinAdapter.built == []
    assert lines[0][1] == NOT_ESTABLISHED
    assert "--swap" in lines[0][2]
    assert PAID_XRP not in lines[0][2], "do not print the other chain's address as though it were the subject"


def test_a_test_port_asks_ONE_read_with_NO_wallet_path_and_renders_the_verdict(monkeypatch):
    """The happy path, behaviorally: one address asked, once, with wallet="".

    NO /wallet/<name> PATH, AND THAT IS NOT AN OMISSION. Measured on the operator's
    Gridcoin v5.5.1.0 daemon 2026-10-03: `help` lists none of createwallet,
    loadwallet, listwallets or unloadwallet, so there is one wallet per datadir and
    no wallet path to ask for. `wallet=""` addresses http://host:port, which is the
    wallet a bare `gridcoinresearchd` CLI call reaches -- the one holding the
    operator's coins, which is exactly why its answer is the evidence.

    MUTATION (ran, caught): pass wallet="desk_hot". The kwargs assertion fails, and
    the URL would become /wallet/desk_hot -- a path this daemon family does not
    serve, so every run would report NOT ESTABLISHED for a reachable daemon.

    MUTATION (ran, caught): call address_ownership twice (once per method, as an
    earlier draft of read_operator_ownership did before it delegated). The
    one-element `asked` assertion fails.
    """
    export_operator_endpoint(monkeypatch)
    RecordingGridcoinAdapter.reset(AddressOwnership(False, "validateaddress answered ismine"))
    monkeypatch.setattr(wallet_custody, "GridcoinAdapter", RecordingGridcoinAdapter)
    desk_said = [(wallet_custody.DESK_DEPOSIT_CHECK, DESK_OWNS, "the desk derived it")]

    lines = wallet_custody.cross_daemon_lines(swap_row(), desk_said)

    assert RecordingGridcoinAdapter.asked == [GRC_DEPOSIT], (
        "exactly one read, for the swap's own deposit address and nothing else"
    )
    assert len(RecordingGridcoinAdapter.built) == 1
    kwargs = RecordingGridcoinAdapter.built[0]
    assert kwargs["wallet"] == "", (
        "the operator's daemon has one wallet per datadir and serves no /wallet/<name> path"
    )
    assert kwargs["port"] == 25715
    assert kwargs["password"] == OPERATOR_CREDENTIAL_CANARY, "basic auth still gets the credential"

    name, state, why = lines[0]
    assert state == SEPARATED
    assert name == f"{wallet_custody.CROSS_DAEMON_CHAIN} operator daemon"
    assert "127.0.0.1:25715" in why
    assert "the other half of this proof" in why, "the desk's recorded DESK_OWNS answer has to reach it"
    assert OPERATOR_CREDENTIAL_CANARY not in why, f"a credential reached a printed report line: {why}"
    assert "µfn" in why, "rule 6: the elapsed figure this line prints is in microfortnights"


def test_the_header_line_announces_the_second_endpoint_without_its_credential(monkeypatch):
    """Rule 14: echo the parameters that decide the answer, BEFORE the socket opens.

    And rule 14's `(none)` half: an unset second endpoint means the GRC separation
    question goes unanswered, so the header says so rather than leaving the reader
    to discover it forty lines down.

    MUTATION (ran, caught): return "" from operator_daemon_note() when the endpoint
    is unset. The variable-naming assertion fails and the header renders a blank
    value, which is ambiguous between "unset" and "this line broke". (A first
    attempt at this mutation was a NO-OP -- `"" or (<the sentence>)` is the
    sentence -- and it is recorded rather than quietly replaced, because a no-op
    mutation reported as SURVIVED sends the next reader looking for a weakness in
    the test that is not there.)

    MUTATION (ran, caught): return f"{endpoint.host}:{endpoint.port} user={endpoint.user}
    pass={endpoint.password}" as a connection string would. The canary assertion fails.
    """
    for name in OPERATOR_REQUIRED_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    note = wallet_custody.operator_daemon_note()
    assert note, "a blank value cannot be told from a broken line"
    for name in OPERATOR_REQUIRED_VARIABLES:
        assert name in note
    assert "NOT asked" in note, "say what does not happen as a result, not only that a variable is unset"

    export_operator_endpoint(monkeypatch)
    note = wallet_custody.operator_daemon_note()
    assert note.startswith("127.0.0.1:25715")
    assert OPERATOR_CREDENTIAL_CANARY not in note, f"a credential reached the header: {note}"
    assert "ismine" in note, "say what the one read against that endpoint is"

    # A MAINNET PORT MUST NOT BE ANNOUNCED AS A READ, and the first version of this
    # header did exactly that: with GRC_OPERATOR_RPC_PORT=15715 it printed
    # "127.0.0.1:15715  <- asked ONE read", promising a read cross_daemon_lines()
    # then refuses to make, about the daemon holding the operator's REAL coins.
    #
    # MUTATION (ran, caught): drop the may_read_a_wallet() call from
    # operator_daemon_note() and always print the "asked ONE read" form. Both
    # assertions below fail.
    export_operator_endpoint(monkeypatch, port=str(CHAIN_PORTS["GRC"].mainnet_port))
    note = wallet_custody.operator_daemon_note()
    assert "WILL NOT BE READ" in note, (
        "a header that announces a read of a MAINNET wallet has announced the thing the tool refuses"
    )
    # may_read_a_wallet()'s sentence is the CHAIN-level one and names GRC_RPC_PORT,
    # the DESK's variable. It is not edited -- there is one mainnet refusal in this
    # tree -- so the borrower has to say which variable is its own, or the operator
    # repoints the desk's endpoint and leaves this one exactly as it was.
    assert OPERATOR_PORT_VARIABLE in note, (
        "the refusal's borrowed sentence names the desk's variable; say which one belongs to THIS "
        "endpoint or the operator changes the wrong line"
    )


def test_the_report_names_no_wallet_WRITE_and_no_credential_FILE_anywhere(monkeypatch):
    """The second endpoint added a connection, not a capability. Walked as an AST.

    This is the companion to test_the_custody_report_names_no_wallet_WRITE_anywhere_in_its_source,
    extended to the files the cross-daemon check added: a credential read from a
    FILE is the thing rule 16 and this repository's own leak history forbid, and
    `wallet.dat` is the file the runbook now warns about first.

    MUTATION (ran, caught): add `open(Path.home() / ".GridcoinResearch" / "gridcoinresearch.conf")`
    to gridcoin_credentials.py to read rpcpassword out of it. The `open` assertion
    fails. MUTATION (ran, caught): add `adapter.call("getnewaddress", "x")` to
    read_operator_ownership(). The method-name assertion fails.
    """
    for relative in ("wallet_custody.py", "swap_terminal/gridcoin_credentials.py"):
        tree = ast.parse((REPO_ROOT / relative).read_text())
        literals = {
            node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            # A docstring or a long sentence is prose; an RPC method or a filename
            # is a bare token. The same split the older test uses, for the reason
            # it records: these docstrings NAME getnewaddress in order to say it is
            # never called.
            and " " not in node.value and "\n" not in node.value
        }
        names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        called = {
            node.func.id for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        for method in WALLET_WRITING_METHODS:
            assert method not in literals, f"{relative} passes {method!r} as an RPC method name"
            assert method not in names, f"{relative} calls .{method}()"
        assert "open" not in called, (
            f"{relative} opens a file. The second endpoint's credential is read from the ENVIRONMENT "
            f"only -- a conf parser here puts an rpcpassword in this process and one careless print "
            f"from a pasted report"
        )
        assert "wallet.dat" not in literals, f"{relative} names wallet.dat as a path it touches"
        assert "read_text" not in names and "open" not in names, (
            f"{relative} reads a file through a path method"
        )
