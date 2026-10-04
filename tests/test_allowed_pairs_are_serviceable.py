#!/usr/bin/env python3
"""Every enabled pair can actually be quoted, or this fails naming what is missing.

Role: tests (read-only)
Reads: config.Config and services/pricing.IDS. No network, no database, no chain.
Writes: nothing
Can move funds: no
Mainnet-safe: yes

CONFIG.ALLOWED_PAIRS IS NOT ENOUGH TO ENABLE A PAIR, and this tree has learned
that twice the expensive way. A pair needs three things beyond membership:

  an adapter      chains/registry.build_adapters() must construct both chains
  a USD price     services/pricing.IDS must carry both assets, or create_quote()
                  accepts the swap and then fails on a missing price
  a fee reserve    config.<TO_ASSET>_NETWORK_FEE_RESERVE must exist, or
                  quote_service.network_fee_reserve() refuses -- it is never
                  defaulted to zero, because zero quotes a payout the
                  destination chain will not deliver

THE SECOND ONE COST A CUSTOMER-FACING ERROR. ("XRP", "GRC") and ("GRC", "XRP")
were added on 2026-09-26 and the operator got this from their browser:

    No quote: 'XRP_NETWORK_FEE_RESERVE'

str(KeyError(...)), rendered to a person. The message is a sentence now, and AS OF
2026-10-03 THE PAIR IS NO LONGER BROKEN: the operator set
XRP_NETWORK_FEE_RESERVE=0.00001, from a figure read off their own rippled
(server_info.validated_ledger.base_fee_xrp, 10 drops) rather than from anybody's
recollection. GRC -> XRP quotes.

THE EXEMPTION THIS FILE CARRIED FOR SEVEN DAYS IS GONE WITH IT. KNOWN_UNQUOTABLE
held exactly one entry from 2026-09-26, and the test guarding it predicted its own
failure and said what to do -- "a tolerated break that outlives its fix is a lie in
the test suite". Both the set and that test are DELETED rather than emptied
(rule 19: a ratchet that reaches zero gets deleted along with its baseline), which
is why the parametrized reserve test below now covers EVERY enabled pair with no
exclusion. That is the property this file always meant to hold.

THE OTHER TWO RESERVES ARE STILL UNMEASURED -- AND THAT SENTENCE IS FALSE AS OF
2026-10-03. It is kept rather than overwritten because its reasoning is what the
measurement refuted (rule 1). It read, in full:

    "THE OTHER TWO RESERVES ARE STILL UNMEASURED and that is named rather than
    tidied away: no BTC or LTC payout has ever been made, so 0.00002 and 0.001 are
    the defaults they always were. Bitcoin's fee is a market rather than a
    constant, so that one wants a fee-rate read and not a typed figure."

Both payouts have since been made, on the operator's host, and the numbers came
back off the chain rather than out of anybody's recollection:

    LTC  payouts.id=19  1.19967368 LTC   and id=20  1.20774577 LTC
    BTC  payouts.id=21  0.0040178 BTC, gettransaction fee 0.00002820

THE LAST CLAUSE WAS RIGHT AND IS WHY THE BTC FIGURE MATTERS. Bitcoin's fee IS a
market and it scales with input count, measured on their host 2026-10-03:

    inputs   fee
    1        0.00002820
    15       0.00021483   (LTC, ~0.0000150/input)
    2701     0.00084240   -- 30x the one-input figure

so a flat constant cannot cover it, and the tree does not rely on one:
services/quote_service.measured_or_configured_reserve() probes the chain with
fundrawtransaction for THIS payout's size, and the constant is only the fallback
for when that probe fails.

WHICH IS WHERE THE DEFECT WAS. BTC_NETWORK_FEE_RESERVE was 2e-05 against a
measured 0.0000282 -- the fallback sat 29% BELOW a fee the chain had actually
charged, on a ONE-input send, which is the cheapest case there is. A reserve below
the real fee is how a payout fails after the deposit is irreversible, and this tree
did exactly that on 2026-10-03 with "Insufficient funds (rpc code -4)". The
operator raised it to 0.0000282 on 2026-10-04 ("yes, please resolve this and make
it the default for btc") -- the lowest value that covers a fee this desk has
actually been charged, which is not a safety margin and must not be described as
one. config.py's line carries the trade-off in both directions.

LTC's 0.001 IS UNTOUCHED and is wrong in the safe direction: ~10x its measured
0.00010372 at one input, so it over-reserves rather than under-reserving. Changing
it is a pricing decision and the operator has not asked (rule 16).
"""

from __future__ import annotations

import sqlite3
from time import time

import pytest
from chains.registry import why_cannot_pay_out
from chains.xrp import XRPAdapter
from chains.xrp_payout_seed import SIGNING_SEED_ENV_VAR
from config import Config
from db import SCHEMA, db_session, dict_factory
from services import pricing
from services.pair_view import pair_serviceability
from services.pricing import IDS
from services.quote_service import create_quote
from valid_addresses import GRC_PAYOUT, xrp_family_seed
from workers.common import get_config_dict

PAIRS = sorted(Config.ALLOWED_PAIRS)

#: KNOWN_UNQUOTABLE IS GONE, 2026-10-03, AND THAT IS THE POINT RATHER THAN A LOSS.
#:
#: It held exactly one entry -- ("GRC", "XRP") -- from 2026-09-26, when the pair was
#: enabled with no XRP fee reserve, until the operator set
#: XRP_NETWORK_FEE_RESERVE=0.00001 from a figure read off their own rippled. The set
#: is empty, so by rule 19 the set and the test that guarded it are DELETED rather
#: than kept as an empty container somebody might refill.
#:
#: The parametrized test below no longer excludes anything, which means it now holds
#: the whole property it always meant to: EVERY enabled pair has a reserve for its
#: payout asset. That is strictly stronger than the version with an exemption, and it
#: is the shape the tolerated break was always a detour around.

#: DELIBERATELY_ONE_WAY IS GONE, 2026-10-04, AND THIS IS THE SECOND TABLE IN THIS
#: FILE TO REACH ZERO AND BE DELETED RATHER THAN EMPTIED (rule 19: a ratchet that
#: reaches zero gets deleted along with its baseline; an empty container is
#: something somebody refills).
#:
#: IT HELD TWO ROWS AT THE END -- ("XRP","BTC") and ("XRP","LTC") -- each reading
#: "the reverse pays out XRP, which needs the two XRP custody variables (the fee
#: reserve is set as of 2026-10-03)". It had held three more for the SOL directions
#: until 2026-10-03, and one for every XRP direction before that.
#:
#: WHAT EMPTIED IT: the operator enabling BTC->XRP, LTC->XRP, SOL->XRP and XRP->SOL
#: on 2026-10-04 ("yeah let's figure out why and enable them"). Config.ALLOWED_PAIRS
#: is now 20 of 20 directed pairs over the five traded assets, so no pair is one-way
#: and there is nothing left to exempt.
#:
#: THE ROWS WERE NOT WRONG, THEY WERE A POSTURE WRITTEN INTO A TABLE OF ABSENCES,
#: which is what the symmetry test below already said about the three SOL rows when
#: they went: "a table of permanent exemptions that records a switch goes stale the
#: moment the switch moves." Both surviving rows named the XRP custody variables --
#: a switch an operator flips -- and the switch moved. The property that replaces
#: them is not nothing and is strictly stronger: the symmetry check below now holds
#: unconditionally, with no table it can be silenced through.
#:
#: WHAT STILL STOPS AN UNARMED HOST OFFERING THESE FOUR is unchanged and is asserted
#: over seeded rows through the real services, not through a pair list:
#: pair_serviceability() marks a pair UNAVAILABLE when its payout leg cannot sign,
#: which is every checkout and every test run -- see
#: test_THE_RESERVE_IS_SET_NOW_AND_THE_PAGE_IS_WHAT_CLOSES_THE_HAZARD_IT_OPENED
#: below and tests/test_solana_adapter.py::
#: test_an_UNARMED_host_REFUSES_a_SOL_payout_swap_so_no_deposit_is_ever_taken.


def test_every_pair_has_a_usd_price_for_both_assets():
    """Without both prices, create_quote() accepts the swap and then cannot price it.

    An accepted swap that cannot be priced is worse than a refused one: the
    customer has been told yes.
    """
    for from_asset, to_asset in PAIRS:
        for asset in (from_asset, to_asset):
            assert asset in IDS, (
                f"({from_asset}, {to_asset}) is enabled but {asset} has no entry in "
                f"services/pricing.IDS, so create_quote() would accept the swap and fail on a "
                f"missing price"
            )


@pytest.mark.parametrize("pair", PAIRS)
def test_EVERY_pair_has_a_network_fee_reserve(pair):
    """The payout asset needs a reserve or the quote refuses.

    MUTATION: add ("BTC", "XRP") to ALLOWED_PAIRS and this fails -- that pair pays
    out XRP and no XRP reserve exists. Verified 2026-09-30; it is the reason the
    two XRP-payout pairs were NOT added alongside the four that were.
    """
    _from_asset, to_asset = pair
    key = f"{to_asset}_NETWORK_FEE_RESERVE"
    assert hasattr(Config, key), (
        f"{pair} is enabled and {key} does not exist, so quote_service.network_fee_reserve() "
        f"refuses every quote for it. A reserve is a pricing decision and is never defaulted to "
        f"zero -- set it, or remove the pair"
    )


def test_the_pair_set_is_SYMMETRIC_with_no_table_it_can_be_silenced_through():
    """Every enabled direction has its reverse enabled too, unconditionally.

    MEASURED 2026-10-04 after the operator enabled the last four directions ("yeah
    let's figure out why and enable them"): Config.ALLOWED_PAIRS is 20 of 20 directed
    pairs over the five traded assets {BTC, GRC, LTC, SOL, XRP}, so the set is
    symmetric and nothing is exempt.

    THIS USED TO READ `and (a, b) not in DELIBERATELY_ONE_WAY`, AND THAT CLAUSE IS
    GONE WITH THE TABLE. The history is worth keeping because the test kept
    predicting it:

      before 2026-10-03  the table held every XRP-payout direction -- "XRP cannot
                         pay out at all", which was an ABSENCE and a fair exemption
      2026-10-03         the three SOL rows went when *->SOL was enabled, and this
                         docstring recorded why: "a table of permanent exemptions
                         that records a switch goes stale the moment the switch
                         moves"
      2026-10-04         the last two rows -- ("XRP","BTC") and ("XRP","LTC") -- went
                         the same way, for the same reason, naming the same kind of
                         thing: the XRP custody variables, which is a switch

    So the table emptied twice by the mechanism it was warned about, and rule 19 says
    what to do with a container that reaches zero: delete it. This assertion is
    strictly stronger than the version that consulted a table, because there is no
    longer anywhere to write a row that silences it.

    A GENUINELY ONE-WAY PAIR WOULD NOW FAIL THIS, which is the intended outcome. The
    honest reason for one is no longer available: no chain in this tree "cannot pay
    out at all" as of 2026-10-03, so an asymmetry is either a mistake or a posture --
    and a posture belongs in pair_serviceability(), which reports it per host, not in
    a constant that reports it for every host forever.

    MUTATION: remove any one pair from Config.ALLOWED_PAIRS and this fails naming it.
    """
    asymmetric = [(a, b) for (a, b) in PAIRS if (b, a) not in Config.ALLOWED_PAIRS]
    assert not asymmetric, (
        f"these pairs are enabled in one direction only: {asymmetric}. As of 2026-10-03 no chain "
        f"here is unable to pay out at all, and as of 2026-10-04 every payout asset has a fee "
        f"reserve, so there is no longer an honest permanent reason for an asymmetry -- it is "
        f"either a mistake in config.py or a per-host posture, and a posture belongs in "
        f"services/pair_view.pair_serviceability() rather than in this set"
    )


def test_the_XRP_PAYOUT_BLOCKER_IS_NOT_THE_RESERVE(monkeypatch):
    """Two blockers, not one, and the comment here named only the cheaper one.

    THIS TEST EXISTS BECAUSE ITS OWN FILE MISLED ME. DELIBERATELY_ONE_WAY said that setting
    XRP_NETWORK_FEE_RESERVE would make all four XRP-payout directions enable-able; on
    2026-09-30 I read that, believed it, and told the operator one environment variable would
    unblock three pairs. It would not. A comment that names the wrong blocker sends the next
    person to do the wrong work, which is why this is a test and not a corrected sentence.

    THE NEWS ARRIVED ON 2026-10-02 AND THIS TEST IS WHERE IT WAS CAUGHT. It used to assert
    `"no signing key" in refusal` and `".env" in refusal`, and it predicted its own failure:
    "If XRP is ever armed, this test starts failing and the failure IS the news." It did fail,
    for exactly that reason, and the body is rewritten rather than relaxed -- rule 2: a test
    changes to pin the STRONGER invariant or it dies with the code it pinned.

    WHAT IS STRONGER NOW. The old version pinned one direction: XRP cannot pay out, full stop.
    The real property has two directions and the second is the one that could regress
    silently:

      unset -> REFUSES   which is the default, every checkout and every test run, and the
                         refusal must NAME the variable so the operator can act on it
                         (rule 14) rather than reading a flat "cannot pay out"
      set   -> PERMITS   so the mechanism is not merely a differently-worded refusal. A guard
                         that refuses in both states would pass the first assertion forever
                         and nobody would notice the payout path was dead.

    ASSERTED FROM THE ADAPTER ITSELF rather than from a list of chains: any asset whose
    adapter cannot pay out is unavailable as a payout leg no matter what else is configured.

    THE SEED HERE DECODES AND CONTROLS NOTHING, AND IT USED TO BE A NON-VALUE. This test
    set "not-a-real-seed-and-never-decoded" while the adapter read only `bool()` of the
    variable; chains/xrp_payout_seed.payout_capability() DECODES it as of 2026-10-03
    (measured that day: a nine-character placeholder on the live host made can_spend True
    and offered a payout that could never have signed), so a non-value now produces the
    UNARMED state and the "set -> PERMITS" half below would have measured its own
    opposite. tests/valid_addresses.xrp_family_seed() derives a seed that really decodes,
    for an account that has never existed on any network and that nothing pays -- so it is
    still never signed with and still reaches no network. The url is unreachable.invalid
    for the same reason: construction makes no call, and if that ever changes this test
    fails loudly.

    A THIRD STATE EXISTS NOW -- the variable set to something that is not a seed -- and it
    is NOT covered here deliberately: tests/test_xrp_payout_wiring.py owns that gate and
    its two refusal sentences. What this test owns is the two-state serviceability
    property it was written for.

    AND THE RESERVE IS STILL NOT THE BLOCKER, which is what this test is named for.
    XRP_NETWORK_FEE_RESERVE is unrelated to either state below and is asserted still absent
    by test_the_KNOWN_unquotable_pair_is_STILL_broken_and_says_so.
    """
    url = "http://unreachable.invalid"

    monkeypatch.delenv(SIGNING_SEED_ENV_VAR, raising=False)
    unarmed = XRPAdapter(url=url, min_confirmations=1)
    assert unarmed.can_spend is False, (
        f"with {SIGNING_SEED_ENV_VAR} unset, XRPAdapter.can_spend must be False -- that is what "
        f"stops services/swap_service.create_swap() taking a deposit it cannot settle"
    )
    refusal = why_cannot_pay_out({"XRP": unarmed}, "XRP")
    assert refusal, "an adapter that cannot spend gave no reason"
    assert SIGNING_SEED_ENV_VAR in refusal, (
        "the refusal no longer names the variable that would fix it, which is the whole of its "
        "usefulness to an operator reading it off a customer page or a spawn banner"
    )
    assert "XRP_DEPOSIT_ACCOUNT" in refusal, (
        "the refusal no longer names the SECOND variable. Both are needed, and naming one sends "
        "the reader to do half the work -- which is the exact mistake this test is named after"
    )

    monkeypatch.setenv(SIGNING_SEED_ENV_VAR, xrp_family_seed("allowed pairs: an armed XRP payout leg"))
    armed = XRPAdapter(url=url, min_confirmations=1)
    assert armed.can_spend is True, (
        f"with {SIGNING_SEED_ENV_VAR} set, XRPAdapter.can_spend must be True. If this fails the "
        f"payout path is unreachable from the worker no matter what the operator exports, and the "
        f"wiring is dead code"
    )
    assert why_cannot_pay_out({"XRP": armed}, "XRP") == "", (
        "an armed adapter still reports a payout refusal, so the customer page would refuse a swap "
        "the worker could actually pay"
    )


def test_THE_RESERVE_IS_SET_NOW_AND_THE_PAGE_IS_WHAT_CLOSES_THE_HAZARD_IT_OPENED(tmp_path):
    """The reserve exists, so the quote PRICES -- and an unarmed host must not offer it.

    THIS TEST WAS test_A_RESERVE_ALONE_WOULD_MOVE_THE_REFUSAL_LATER_NOT_REMOVE_IT and
    it asserted that XRP_NETWORK_FEE_RESERVE was ABSENT. The operator set it on
    2026-10-03 from a figure read off their own rippled
    (server_info.validated_ledger.base_fee_xrp, 10 drops), so that assertion is now
    false and the test is rewritten to pin the stronger invariant rather than
    relaxed (rule 2).

    WHAT THE OLD TEST MEASURED, AND IT WAS RIGHT AT THE TIME:

        without XRP_NETWORK_FEE_RESERVE   create_quote() refuses
        with it set to 0.00001            create_quote() SUCCEEDS and create_swap()
                                          refuses -- "XRP cannot pay out: it holds
                                          no signing key"

    and it concluded the reserve "moves the refusal from BEFORE the promise to AFTER
    it: a teller quotes a customer a number and then cannot open the swap. That is
    strictly worse than today." That reasoning is why the reserve was not added for
    three days, and it was correct about the arithmetic.

    WHAT CHANGED IS NOT THE ARITHMETIC, IT IS THE PAGE. On 2026-10-03
    services/pair_view.pair_serviceability() gained a fourth condition, so a pair is
    offered only when it can also be QUOTED -- and the three it already asked
    include whether the destination can pay out. On an UNARMED host XRP cannot pay
    out, so GRC -> XRP reads UNAVAILABLE and the customer form never offers it. The
    quote-then-refuse path the old test feared is no longer reachable from the
    surface a customer uses.

    So this asserts the two halves that now matter together, because either alone is
    the old hazard:

      the quote PRICES          the reserve is real and does its job
      the page REFUSES the pair unarmed, so nobody is quoted a number they cannot be
                                paid

    MUTATION: drop `cannot_pay` from pair_serviceability()'s conjunction. The quote
    still prices, the page starts offering the pair, and the second assertion fails
    -- which is exactly the teller-quotes-then-cannot-open sequence, reachable
    again.
    """
    if ("GRC", "XRP") not in Config.ALLOWED_PAIRS:
        pytest.skip("GRC->XRP is no longer enabled, so there is nothing to measure here")

    db_path = tmp_path / "reserve.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = dict_factory
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()

    now = time()
    pricing._cache.update({
        "raw": {cg: {"usd": 100.0, "usd_market_cap": 5_000_000_000.0,
                     "usd_24h_vol": 200_000_000.0, "usd_24h_change": 1.0,
                     "last_updated_at": 1790717713} for cg in IDS.values()},
        "prices": None, "context": None, "source": "seeded",
        "fetched_at": now, "expires_at": now + 999,
    })
    config = dict(get_config_dict())
    config["DB_PATH"] = str(db_path)

    assert "XRP_NETWORK_FEE_RESERVE" in config, (
        "the reserve is gone again. If that was deliberate, this test should go back to the version in "
        "git history that asserted its absence -- but a pair enabled with no reserve refuses every quote"
    )

    # HALF ONE: the quote prices. The reserve is doing its job.
    with db_session(str(db_path)) as db:
        quote = create_quote(db, config, "GRC", "XRP", 10)
    assert quote["output_amount_estimate"] > 0, (
        "the quote priced to zero, so this measures nothing about what a customer is told"
    )

    # HALF TWO: with no seed exported, the page does not offer the pair -- so the
    # number above is never shown to anybody. THE SEED HERE IS NOT A SEED and none is
    # set: the adapter reads only bool() of the variable
    # (chains/xrp_payout_seed.signing_seed_is_present()), the url is unreachable, and
    # construction makes no call.
    verdict = pair_serviceability(
        config,
        {"GRC": _Payable(), "XRP": XRPAdapter(url="http://unreachable.invalid", min_confirmations=1)},
        "GRC", "XRP",
    )
    assert verdict["serviceable"] is False, (
        "an unarmed host offered GRC -> XRP. The quote prices now, so offering it is the "
        "teller-quotes-a-number-then-cannot-open-the-swap sequence this test is named after"
    )
    assert verdict["cannot_pay"], (
        f"the pair was refused for something other than the payout, so this test is not measuring the "
        f"unarmed case: {verdict['reason'][:120]}"
    )
    assert not verdict["cannot_quote"], (
        "the pair is still refused for want of a fee reserve, so the operator's 2026-10-03 setting did "
        "not reach this config"
    )


class _Payable:
    """A GRC chain that can pay out and validate, so only XRP's posture is measured."""

    asset, can_spend = "GRC", True

    def get_new_address(self, _label):
        return GRC_PAYOUT

    def validate_address(self, _address):
        return True

    def describe_address(self, _address):
        return "a stub address"

    def why_cannot_pay_out(self):
        return ""

    def owns_address(self, address):
        """Not the desk's -- these fixtures all supply a CUSTOMER payout address.

        Added 2026-10-04 with the ownership gate in services/swap_service.
        refuse_unusable_payout_address(). A destination stub without it raises
        AttributeError, so the gate could not be exercised and every create_swap
        test failed on the harness instead of on its own subject. False is the
        honest default here; a test that wants the refusal returns True.
        """
        return False


#: THE FOUR DIRECTIONS ENABLED 2026-10-04, as (from, to). Spelled here rather than
#: derived, deliberately and for once: the subject of the test below is "the operator
#: asked for exactly these four and exactly these four went in", and a derivation from
#: Config.ALLOWED_PAIRS would assert that whatever is in the set is in the set.
#:
#: THE OPERATOR'S WORDS: they asked why BTC->XRP, LTC->XRP, SOL->XRP and XRP->SOL were
#: not in Config.ALLOWED_PAIRS, and then "yeah let's figure out why and enable them".
#: That is live posture, authorized in those words (rule 16), and it takes the set from
#: 16 of 20 directed pairs over the five traded assets to 20 of 20.
ENABLED_2026_10_04 = (("BTC", "XRP"), ("LTC", "XRP"), ("SOL", "XRP"), ("XRP", "SOL"))


@pytest.mark.parametrize("pair", ENABLED_2026_10_04)
def test_the_four_directions_the_operator_asked_for_are_enabled(pair):
    """Each of the four, with the prerequisite that had blocked it asserted alongside.

    MEASURED 2026-10-04 by importing the real Config and the real services/pricing.IDS,
    which is the denominator this file exists to state -- every one of the five traded
    assets has all three of the things a pair needs, so no pair among them is blocked:

        asset  fee reserve   pricing.IDS id       MIN_CONFIRMATIONS
        BTC    2e-05         bitcoin              2
        GRC    0.001         gridcoin-research    6
        LTC    0.001         litecoin             2
        SOL    5e-06         solana               3
        XRP    1e-05         ripple               1

    (BTC's reserve became 0.0000282 later the same day, for a reason that has nothing
    to do with these pairs -- see tests/test_measured_fee_reserve.py. The reading above
    is as taken, at the moment the pairs went in.)

    WHY EACH WAS ABSENT, AND WHEN THAT STOPPED BEING TRUE. None of it is a config gap
    any more, and every clause was checked rather than recalled:

      BTC->XRP  needed XRP_NETWORK_FEE_RESERVE, which did not exist. Set 2026-10-03,
      LTC->XRP  to 0.00001, off the operator's own rippled
                (server_info.validated_ledger.base_fee_xrp, 10 drops).
      SOL->XRP  the same reserve, plus a claim that chains/xrp.py "holds no signing key
                and services/payout_service.py calls send_to_address() without the
                arming token". False since 2026-10-02: broadcast_payout() passes
                source=, seed= and confirm_send=CONFIRM_XRP_SEND for XRP, and a real
                XRP payout settled 2026-10-03 (payouts.id=23, tesSUCCESS, validated).
      XRP->SOL  needed SOL_NETWORK_FEE_RESERVE (set 2026-10-03 to 0.000005, measured
                through getFeeForMessage on the operator's own cluster) and a SOL
                signing path, which landed the same day in 79c4808.

    THIS DOES NOT ASSERT THAT ANY OF THEM CAN BE SWAPPED HERE, and that distinction is
    the one this file has drawn since it was written: three gates, not one. ALLOWED is
    what this set says; CREATABLE additionally needs the FROM chain's deposit account
    (XRP_DEPOSIT_ACCOUNT, SOL_DEPOSIT_ACCOUNT); ARMED needs the payout leg's key
    (XRP_PAYOUT_SECRET_SEED, or SOL_PAYOUT_KEYPAIR_PATH plus a funded SOL_HOT_WALLET).
    None of those five is set in any checkout, so pair_serviceability() marks all four
    UNAVAILABLE and the customer form never offers them --
    test_THE_RESERVE_IS_SET_NOW_AND_THE_PAGE_IS_WHAT_CLOSES_THE_HAZARD_IT_OPENED above
    asserts that half over seeded rows through the real services.

    MUTATION: remove any one of the four from Config.ALLOWED_PAIRS and this fails
    naming it, as does the parametrized reserve test and the symmetry test.
    """
    from_asset, to_asset = pair
    assert pair in Config.ALLOWED_PAIRS, (
        f"{pair} was authorized by the operator on 2026-10-04 and is not in Config.ALLOWED_PAIRS"
    )
    assert hasattr(Config, f"{to_asset}_NETWORK_FEE_RESERVE"), (
        f"{pair} is enabled and {to_asset}_NETWORK_FEE_RESERVE does not exist, which is the exact "
        f"blocker that kept it out until 2026-10-03"
    )
    for asset in pair:
        assert asset in IDS, f"{pair} is enabled and {asset} has no services/pricing.IDS row"
    assert (to_asset, from_asset) in Config.ALLOWED_PAIRS, (
        f"{pair} went in without its reverse, so the set is asymmetric and no table exempts it any more"
    )


def test_the_pair_set_is_exactly_twenty_of_twenty_over_five_assets():
    """The denominator, asserted rather than written in a comment somewhere (rule 3).

    MEASURED 2026-10-04: 5 traded assets, 20 ordered pairs, 20 in
    Config.ALLOWED_PAIRS. Stated as a product of the asset count rather than as a
    literal 20, so adding a sixth asset makes this fail with arithmetic a reader can
    follow -- a bare `== 20` would have to be found and edited, and a count whose
    denominator is not stated beside it is the error rule 3 names.

    WHY A COUNT AT ALL, when the symmetry test above covers the shape: because
    symmetry holds for a smaller set too. Sixteen of twenty was symmetric on
    2026-10-03 -- the four missing directions were two symmetric pairs -- so symmetry
    alone cannot tell that pair set from this one. This is what says the set is
    COMPLETE and would fail if a direction were quietly dropped in a pair.
    """
    assets = {asset for pair in Config.ALLOWED_PAIRS for asset in pair}
    expected = len(assets) * (len(assets) - 1)
    assert len(Config.ALLOWED_PAIRS) == expected, (
        f"Config.ALLOWED_PAIRS holds {len(Config.ALLOWED_PAIRS)} of the {expected} ordered pairs over "
        f"its {len(assets)} assets {sorted(assets)}. Every direction was enabled on 2026-10-04, so a "
        f"shortfall means one was removed -- which is live posture and belongs to the operator"
    )
