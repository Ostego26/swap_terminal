"""Can the destination wallet actually fund this payout. ONE function, two callers.

Role: submodule (one decision function; one read-only RPC call, no writes)
Reads: the destination chain's adapter (get_balance), nothing else. No database,
      no environment, no file.
Writes: nothing
Can move funds: no
Live-safe: yes -- get_balance() is a read. It holds no passphrase, derives no
      key, and signs nothing.

WHY THIS EXISTS, MEASURED ON THE OPERATOR'S HOST 2026-10-03.

The first BTC -> GRC swap this terminal ever ran took its deposit and could not
pay. Everything up to the payout was correct:

    deposit    0.001 BTC, 2 of 2 confirmations, COUNTED by the gate, credited
               12:46:16 -- irreversible
    payout     9049.68583412 GRC claimed at 12:46:27
    result     failed -- "Insufficient funds (rpc code -4)" from the Gridcoin
               daemon, which was right

    need       9049.68583412 GRC
    have       3780.08854497 GRC spendable
    short      5269.59728915 GRC -- the wallet held 41.8% of what was quoted

Nothing in the tree had compared those two numbers. Grepped the same day:
services/quote_service.py and services/swap_service.py contained ZERO calls to
get_balance(), so the payout's funding was first tested by the daemon, at the
one moment when refusing costs a customer their deposit rather than a retry.
swap_readiness.py asked `GRC balance > 0` -- "must be > 0 to pay a GRC leg" --
which is the same necessary-and-not-sufficient shape as "an adapter that
CONNECTS is not a wallet that can act", one layer out.

The ordering is the whole defect. create_swap() already carries four refusals
whose shared reasoning is "refusing before the swap row exists is the only stage
at which nothing has been taken" -- no adapter, cannot sign, no payout source
account, fee cannot cover its own payout. This is the fifth, and it is the one
the operator authorized on 2026-10-03 after being shown the numbers above
(CLAUDE.md rule 16: a gate that decides whether a swap may be created is live
posture, measured and handed over rather than shipped quietly).

WHY A SEPARATE MODULE AND NOT services/payout_service.py, WHICH OWNS THE OTHER
BALANCE READ. payout_service imports services/swap_service (line 93:
SHARED_ACCOUNT_PAYOUT_ASSETS, payout_source_account, set_swap_status), and
swap_service is this gate's main caller, so putting it there would be an import
cycle. A leaf that imports nothing from services/ can be called from
create_quote(), create_swap(), open_swap.py and swap_readiness.py alike, which
is rule 10's reason for putting the deciding thing at the bottom.

WHY IT TAKES `reserve` RATHER THAN READING IT. quote_service.
get_network_fee_reserve() is the authority for the reserve and this module must
not become a second one (rule 8); taking it as an argument also keeps this leaf
free of the services/ imports the paragraph above is about. Every caller already
holds a config.

WHY THE RESERVE IS ADDED RATHER THAN SUBTRACTED. It is not taken out of the
payout -- services/quote_service.create_quote() stopped doing that, and
services/swap_service.create_swap()'s fee-floor guard records the measurement
that required it. So the wallet has to hold the payout AND the chain fee that
sends it, which is what the daemon was actually short of.
"""

from __future__ import annotations

import logging
from typing import NamedTuple

logger = logging.getLogger(__name__)


class FundingVerdict(NamedTuple):
    """Three states, because two of them are not the same refusal.

    THE SHAPE IS modules/address_authority.check_address()'s, DELIBERATELY. That
    function is called twenty lines from this one inside create_swap() and already
    taught this codebase the distinction: `refuses` is a verdict, `unchecked` is
    the absence of one, and collapsing them is how a gate comes to report "fine"
    for a question it never asked. A second vocabulary for the same three states
    would be rule 8's duplicate with a delay on it.

      refuses    the wallet cannot fund this payout, or its balance did not read.
                 `why` is the sentence to act on.
      unchecked  this adapter cannot be asked at all. `why` says so, the caller
                 WARNS and proceeds, and the daemon remains the authority -- the
                 same handling payout_verdict.unchecked gets in create_swap().
      neither    the wallet holds it. `why` is "".
    """

    refuses: bool
    unchecked: bool
    why: str


FUNDABLE = FundingVerdict(refuses=False, unchecked=False, why="")


def why_the_payout_cannot_be_funded(
    adapters, asset: str, amount: float, reserve: float, source_account: str = ""
) -> FundingVerdict:
    """FUNDABLE when this wallet can fund `amount` of `asset`, else a verdict with the sentence.

    NEVER RAISES, because every caller is deciding whether to refuse and a caller
    that has to wrap this in a try/except would be free to get that wrapping
    wrong in four places.

    A FAILED BALANCE READ REFUSES, and that direction is deliberate rather than
    defensive. The two costs are not symmetric: fail-open means a swap is
    created, a deposit is taken and confirmed, and the payout is the thing that
    discovers the problem -- which is the exact 2026-10-03 failure this module is
    named after. Fail-closed costs a retry. So an unreadable balance is reported
    as an unreadable balance, in its own words, and the sentence says which of
    the two happened so the remedies stay distinguishable:

        "... holds 3780.08854497 GRC ..."    the wallet is short
        "... could not be read ..."          the daemon did not answer

    That distinction is CLAUDE.md rule 12's BLE001 note at its root: a broad
    catch is never legitimate when the caller cannot tell the failure from a real
    answer. Here the caller refuses either way, and the OPERATOR is the one who
    must be able to tell them apart.

    NO ADAPTER IS NOT THIS FUNCTION'S QUESTION and returns "". chains/registry.
    unconfigured_chains() is the authority for that and create_swap() asks it
    first, with a message naming the variable to export; answering it a second
    time here would be two sentences for one cause (rule 8).
    """
    adapter = adapters.get(asset)
    if adapter is None:
        return FUNDABLE
    # WHICH BALANCE. get_balance() IS NOT THE QUESTION ON EVERY CHAIN, AND ASSUMING
    # IT WAS BROKE XRP WITHIN MINUTES OF THIS GATE BEING WRITTEN.
    #
    # tests/test_xrp_payout_wiring.py::test_a_swap_is_created_once_both_variables_are_set
    # failed with the adapter's own refusal, quoted in full because it is the
    # explanation:
    #
    #     "get_balance() answers 'what is MY balance' and this adapter has no account
    #      of its own, deliberately ... The account XRP payouts are debited from is
    #      XRP_DEPOSIT_ACCOUNT, supplied at the call site by services/payout_service.py,
    #      and account_balance(address) is how to read one. Nothing gates a payout on
    #      this call."
    #
    # So a gate built on get_balance() would have refused EVERY XRP-destination swap
    # on a deliberate refusal from a method that was never the right question -- a
    # posture change far larger than the one the operator authorized, arriving as a
    # side effect of a method name. chains/solana.py has the same shape when
    # SOL_HOT_WALLET is unset, which services/payout_service.py:804 already records.
    #
    # `source_account` IS services/swap_service.payout_source_account()'s OWN
    # CONTRACT, passed in rather than re-derived: "" for BTC, LTC and GRC, whose
    # daemon picks the inputs and whose get_balance() therefore IS the payable
    # balance, and a named account for SHARED_ACCOUNT_PAYOUT_ASSETS. That function is
    # already called twenty lines earlier in create_swap(), so the discriminator is
    # the one the payout itself uses and cannot drift from it (rule 8). It is a
    # parameter and not an import because swap_service imports THIS module, and a
    # leaf that imports its caller is a cycle.
    #
    # NOT COVERED, SAID OUT LOUD RATHER THAN LEFT AS A GAP (rule 16: a fix that
    # cannot be tested here is a proposal). Reading a named account's balance is
    # account_balance(address) for XRP and SOL_HOT_WALLET for Solana; neither can be
    # exercised in this environment, which has no XRP or SOL endpoint, so neither is
    # implemented here and both are reported as NOT CHECKED. An XRP or SOL
    # destination therefore has exactly the funding verification it had before this
    # module existed -- none -- and now says so.
    if source_account:
        return FundingVerdict(
            refuses=False, unchecked=True,
            why=(f"{asset} payouts are debited from a named account ({source_account}) rather than from "
                 f"this adapter's own wallet, and reading that account's balance is not implemented "
                 f"here, so whether it can fund {amount} {asset} was NOT established. For XRP that read "
                 f"is account_balance(XRP_DEPOSIT_ACCOUNT); for SOL it is SOL_HOT_WALLET"),
        )
    # AN ADAPTER THAT HAS NO get_balance AT ALL IS A DIFFERENT QUESTION FROM A
    # BALANCE THAT DID NOT READ, and the two get different answers.
    #
    # Counted 2026-10-03: three adapter classes serve all five chains and every one
    # of them implements it -- chains/base.py:346 (BTC, LTC, GRC), chains/
    # solana.py:1008, chains/xrp.py:518. So this branch cannot fire in this process
    # as it is built, and the `unchecked` path is not a hole in the gate; it is what
    # a FUTURE adapter shape that cannot report a balance would take. Refusing those
    # would be this gate deciding a chain may not trade on the strength of a method
    # it does not implement, which is a posture change nobody asked for and is not
    # what the 2026-10-03 incident was about -- there the balance was perfectly
    # readable and nothing read it.
    #
    # It is reported rather than passed over, because a gate that silently answers
    # "fine" to a question it could not ask is the defect this whole module exists
    # to remove, one level up.
    if not hasattr(adapter, "get_balance"):
        return FundingVerdict(
            refuses=False, unchecked=True,
            why=(f"the {asset} adapter ({type(adapter).__name__}) has no get_balance(), so whether it "
                 f"can fund {amount} {asset} was NOT established here. The daemon remains the "
                 f"authority, and it answers at payout time -- after the deposit is irreversible"),
        )
    try:
        spendable = float(adapter.get_balance())
    except Exception as error:  # noqa: BLE001 -- checked: this REFUSES, with the type and message in the returned sentence, so no caller can mistake it for a fundable wallet. See the fail-closed paragraph above.
        return FundingVerdict(
            refuses=True, unchecked=False,
            why=(f"the {asset} payout wallet's balance could not be read, so whether it can fund "
                 f"{amount} {asset} was NOT established ({type(error).__name__}: {error}). Refusing "
                 f"rather than taking a deposit on an unverified wallet -- a payout that fails arrives "
                 f"AFTER the deposit is confirmed and irreversible"),
        )
    needed = amount + reserve
    if spendable >= needed:
        return FUNDABLE
    return FundingVerdict(refuses=True, unchecked=False, why=(
        f"the {asset} payout wallet holds {spendable} {asset} spendable and this payout needs "
        f"{needed} {asset} ({amount} to the customer plus {reserve} for the transaction's own chain "
        f"fee), so it is short {needed - spendable} {asset}. The daemon would refuse this with "
        f"'Insufficient funds' AFTER the deposit was confirmed and irreversible, which is what "
        f"happened on 2026-10-03. Fund the wallet, or swap an input small enough that the payout fits"
    ))


def largest_fundable_payout(adapters, asset: str, reserve: float) -> tuple[float, str]:
    """The biggest payout this wallet could fund right now. (amount, how it was read).

    SEPARATE FROM THE GATE ABOVE because it answers a question with no amount in
    it, which is what every PREVIEW needs: swap_readiness.py has no swap and
    open_swap.py's dry run has an input but deliberately computes no payout
    (the rate is fixed by create_quote() at --apply time, and a second figure
    there would be a second copy of the fee arithmetic).

    Returns a NEGATIVE-FREE amount and a sentence saying where it came from, so a
    caller printing it can state what the number means next to the number
    (rule 14) instead of printing a bare float an operator has to interpret.

    (-1.0, reason) MEANS NOT ESTABLISHED -- no adapter, or the balance did not
    read. A caller must print the reason rather than the number; -1.0 is chosen
    over 0.0 precisely because 0.0 is a legitimate answer for an empty wallet and
    the two must not render the same way (rule 13: "did nothing" must not look
    like "did work").
    """
    adapter = adapters.get(asset)
    if adapter is None:
        return -1.0, f"no {asset} adapter in this process, so no {asset} balance was read"
    try:
        spendable = float(adapter.get_balance())
    except Exception as error:  # noqa: BLE001 -- checked: returns the NOT-ESTABLISHED sentinel with the error in its reason, so a caller cannot print it as a ceiling.
        return -1.0, (f"the {asset} balance could not be read ({type(error).__name__}: "
                      f"{str(error)[:120]}), so the ceiling is NOT established")
    return max(spendable - reserve, 0.0), (
        f"{spendable} {asset} spendable less {reserve} {asset} reserved for the payout transaction's "
        f"own chain fee"
    )
