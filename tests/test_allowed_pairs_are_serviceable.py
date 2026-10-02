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

str(KeyError(...)), rendered to a person. The message is a sentence now, but the
PAIR IS STILL BROKEN: no XRP reserve exists, so every GRC->XRP quote refuses,
four days later. Setting that number is a pricing decision and the operator's
(rule 16) -- so this test does not invent one. It fails, by name, until the
number exists or the pair is removed.

That is deliberate: a failing test is how "we enabled a pair nobody can trade"
stops being invisible. It is NOT a ratchet and has no baseline file (rule 19) --
there is one known instance, it is named in the assertion, and the test goes
green the moment either half is resolved.
"""

from __future__ import annotations

import sqlite3
from time import time

import pytest
from chains.registry import why_cannot_pay_out
from chains.xrp import XRPAdapter
from config import Config
from db import SCHEMA, db_session, dict_factory
from services import pricing
from services.pricing import IDS
from services.quote_service import create_quote
from services.swap_service import create_swap
from valid_addresses import GRC_PAYOUT, XRP_HOT_ACCOUNT
from workers.common import get_config_dict

PAIRS = sorted(Config.ALLOWED_PAIRS)

#: The pair enabled without a reserve, and the only one this test tolerates as a
#: KNOWN break -- listed so the failure names it as known rather than as new. Any
#: OTHER pair missing a reserve is an unknown break and fails the strict test.
#:
#: IT IS BROKEN TWICE OVER, and until 2026-09-30 this only recorded the first:
#: XRP has no fee reserve (so the quote refuses) AND XRP cannot pay out at all
#: (so the swap would refuse even with one). See DELIBERATELY_ONE_WAY below for
#: the measurement, and test_the_XRP_PAYOUT_BLOCKER_IS_NOT_THE_RESERVE for the
#: assertion. Fixing the reserve alone would not make this pair work -- it would
#: make it fail LATER, after a customer had been quoted a number.
KNOWN_UNQUOTABLE = {("GRC", "XRP")}

#: PAIRS THAT ARE DELIBERATELY ONE-WAY, each with the reason. This test caught my
#: own asymmetry on the commit that added these two, which is what it is for.
#:
#: XRP->BTC and XRP->LTC take XRP as the INPUT and pay out to an asset that can
#: pay. The reverse of each -- BTC->XRP, LTC->XRP -- would pay out IN XRP, and XRP
#: cannot pay out at all.
#:
#: THE SENTENCE THAT WAS HERE NAMED THE WRONG BLOCKER AND SENT ME TO DO THE WRONG
#: WORK. It said: "When XRP_NETWORK_FEE_RESERVE is set, all four XRP-payout
#: directions become enable-able at once and this table should empty." That is
#: false, and on 2026-09-30 I read it, believed it, and told the operator that
#: setting one environment variable would unblock three pairs. MEASURED instead:
#:
#:   without XRP_NETWORK_FEE_RESERVE   create_quote() refuses -- "XRP has no
#:                                     network fee reserve"
#:   with it set to 0.00001            create_quote() SUCCEEDS (9.84999 XRP on a
#:                                     10 GRC input) and create_swap() refuses --
#:                                     "XRP cannot pay out: it holds no signing
#:                                     key, and services/payout_service.py calls
#:                                     send_to_address() without the arming token"
#:
#: So the reserve does not unblock the pair. It moves the refusal from BEFORE the
#: promise to AFTER it: a teller quotes a customer a number and then cannot open
#: the swap. That is strictly worse than today, which is why the reserve is not
#: being added here and why it would not help if it were.
#:
#: WHAT WOULD ACTUALLY UNBLOCK THESE: arming the XRP payout -- a signing key
#: reaching payout_service's send_to_address() call site. chains/registry's own
#: refusal says "Nothing in a .env can arm it", it is fund movement, and rule 16
#: puts it with the operator. The reserve is ALSO needed, and is a pricing
#: decision they own as well (services/quote_service.get_network_fee_reserve()
#: refuses rather than defaulting to zero, deliberately). Two operator decisions,
#: not one environment variable.
DELIBERATELY_ONE_WAY = {
    ("XRP", "BTC"): "the reverse pays out XRP, which cannot pay out at all (no signing key)",
    ("XRP", "LTC"): "the reverse pays out XRP, which cannot pay out at all (no signing key)",
    # SOL -> GRC, 2026-10-01. The reverse is blocked TWICE, and this test is where that gets
    # recorded so nobody fixes one half and expects a working pair -- which is the mistake the
    # paragraph above this table documents me making about XRP.
    #
    #   no SOL_NETWORK_FEE_RESERVE   config.py has BTC, LTC and GRC only. Setting it is a
    #                                pricing decision and the operator's (rule 16).
    #   no SOL send path             chains/solana.py's send_to_address() raises. It holds no
    #                                keypair and imports nothing that could, so this is an
    #                                ABSENCE and not a flag -- "nothing in a .env can arm it",
    #                                in chains/registry's own words about XRP.
    #
    # Either one alone leaves ("GRC", "SOL") broken, so both are named. The forward direction
    # needs neither: the reserve is the TO asset's (GRC's, which exists) and the payout is in
    # GRC, which pays out today.
    ("SOL", "GRC"): ("the reverse pays out SOL, which has no fee reserve AND cannot pay out "
                     "at all (send_to_address raises; no keypair in the module)"),
}


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


@pytest.mark.parametrize("pair", [p for p in PAIRS if p not in KNOWN_UNQUOTABLE])
def test_every_pair_NOT_known_broken_has_a_network_fee_reserve(pair):
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


def test_the_KNOWN_unquotable_pair_is_STILL_broken_and_says_so():
    """This fails when the break is FIXED, which is the point.

    ("GRC", "XRP") has been enabled and unquotable since 2026-09-26. When
    XRP_NETWORK_FEE_RESERVE is set, this test fails and KNOWN_UNQUOTABLE should
    be emptied -- a tolerated break that outlives its fix is a lie in the test
    suite, and rule 19 says a ratchet that reaches zero gets deleted along with
    its baseline.
    """
    for _from_asset, to_asset in sorted(KNOWN_UNQUOTABLE):
        key = f"{to_asset}_NETWORK_FEE_RESERVE"
        assert not hasattr(Config, key), (
            f"{key} now exists, so ('GRC', '{to_asset}') is quotable and is no longer a known "
            f"break. Remove it from KNOWN_UNQUOTABLE -- and if that empties the set, delete it "
            f"and this test with it"
        )


def test_the_pair_set_is_SYMMETRIC_or_says_which_direction_is_missing():
    """A one-way pair is legitimate and should be deliberate, not accidental.

    SOL can only ever be an INPUT -- chains/solana.py's send_to_address raises
    NotImplementedError -- so a SOL pair would correctly be one-way. Nothing
    enabled today is, and this reports any asymmetry rather than asserting
    against it, because the honest answer depends on which chain can pay out.
    """
    asymmetric = [(a, b) for (a, b) in PAIRS
                  if (b, a) not in Config.ALLOWED_PAIRS and (a, b) not in DELIBERATELY_ONE_WAY]
    assert not asymmetric, (
        f"these pairs are enabled in one direction only: {asymmetric}. That is legitimate when the "
        f"reverse chain cannot pay out (SOL, whose send_to_address raises) or the reverse "
        f"payout asset has no fee reserve, and a mistake otherwise. If it is deliberate, add it "
        f"to DELIBERATELY_ONE_WAY with the reason"
    )


def test_every_DELIBERATELY_one_way_pair_is_actually_enabled_and_actually_one_way():
    """The exemption table cannot outlive what it exempts.

    Two ways it goes stale and both are silent: a row for a pair nobody enabled
    any more, and a row for a pair whose reverse was since enabled -- the second
    of which would hide a real asymmetry behind a stale excuse.
    """
    for pair, reason in sorted(DELIBERATELY_ONE_WAY.items()):
        assert pair in Config.ALLOWED_PAIRS, (
            f"{pair} is exempted from the symmetry check and is not enabled at all. Remove the row"
        )
        reverse = (pair[1], pair[0])
        assert reverse not in Config.ALLOWED_PAIRS, (
            f"{reverse} is now enabled, so {pair} is no longer one-way and its exemption "
            f"({reason}) is stale. Remove the row"
        )


def test_the_XRP_PAYOUT_BLOCKER_IS_NOT_THE_RESERVE():
    """Two blockers, not one, and the comment here named only the cheaper one.

    THIS TEST EXISTS BECAUSE ITS OWN FILE MISLED ME. DELIBERATELY_ONE_WAY said that setting
    XRP_NETWORK_FEE_RESERVE would make all four XRP-payout directions enable-able; on
    2026-09-30 I read that, believed it, and told the operator one environment variable would
    unblock three pairs. It would not. A comment that names the wrong blocker sends the next
    person to do the wrong work, which is why this is a test and not a corrected sentence.

    ASSERTED FROM THE ADAPTER ITSELF rather than from a list of chains: any asset whose
    adapter cannot pay out is unavailable as a payout leg no matter what else is configured,
    and the reason has to say so. If XRP is ever armed, this test starts failing and the
    failure IS the news -- at which point DELIBERATELY_ONE_WAY can empty, for the right
    reason.
    """
    adapter = XRPAdapter(url="http://unreachable.invalid", min_confirmations=1)
    assert adapter.can_spend is False, (
        "XRPAdapter.can_spend is no longer False. If the payout is armed, say so here and in "
        "DELIBERATELY_ONE_WAY -- and check that the reserve exists before enabling a pair"
    )
    refusal = why_cannot_pay_out({"XRP": adapter}, "XRP")
    assert refusal, "an adapter that cannot spend gave no reason"
    assert "no signing key" in refusal, "the reason no longer names the actual blocker"
    assert ".env" in refusal, (
        "the reason no longer says that configuration cannot fix this, which is the sentence "
        "that stops somebody adding a reserve and expecting the pair to work"
    )


def test_A_RESERVE_ALONE_WOULD_MOVE_THE_REFUSAL_LATER_NOT_REMOVE_IT(tmp_path):
    """MEASURED, not reasoned: with a reserve the quote SUCCEEDS and the swap refuses.

    That is worse than today rather than better. The current refusal happens before anything
    is promised; with a reserve set, a teller reads a number out to a customer and then cannot
    open the swap. This is the evidence behind not adding XRP_NETWORK_FEE_RESERVE, and it is
    behavioral -- seeded rows through the real services, per swap_terminal/CLAUDE.md.

    MUTATION: add XRP_NETWORK_FEE_RESERVE to config.py. This test still passes, because it
    supplies its own -- what changes is that the operator's quote path starts promising XRP
    payouts it cannot deliver, which is exactly what this records as the cost.
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

    # AS IT IS TODAY: the quote refuses, early, naming the setting.
    assert "XRP_NETWORK_FEE_RESERVE" not in config, (
        "XRP_NETWORK_FEE_RESERVE has been added to config.py. Read this test before keeping "
        "it: a reserve moves the XRP refusal from the quote to the swap, and the swap refusal "
        "arrives after a customer has been quoted a number"
    )
    # THE REFUSAL'S REASON CHANGED on 2026-10-02 while its shape did not. The reserve
    # is no longer subtracted from a payout (quote_service.create_quote has the
    # measurement), so a missing one is no longer "the chain will not deliver this"
    # -- it is "nobody has recorded what a payout on this chain costs, so the margin
    # is unknown". Matched on that, because matching the old sentence would be
    # asserting a justification this repository has retracted.
    with db_session(str(db_path)) as db, pytest.raises(ValueError, match="costs this desk"):
        create_quote(db, config, "GRC", "XRP", 10)

    # WITH A RESERVE SUPPLIED: the quote succeeds and the SWAP is what refuses.
    config["XRP_NETWORK_FEE_RESERVE"] = 0.00001

    class _Payable:
        asset, can_spend = "GRC", True

        def get_new_address(self, _label):
            return GRC_PAYOUT

        def validate_address(self, _address):
            return True

        def describe_address(self, _address):
            return "a stub address"

        def why_cannot_pay_out(self):
            return ""

    with db_session(str(db_path)) as db:
        quote = create_quote(db, config, "GRC", "XRP", 10)
        assert quote["output_amount_estimate"] > 0, (
            "the quote priced to zero, so this measures nothing about what a customer is told"
        )
        with pytest.raises(ValueError, match="cannot pay out"):
            create_swap(db, config, {"GRC": _Payable(),
                                     "XRP": XRPAdapter(url="http://unreachable.invalid",
                                                       min_confirmations=1)},
                        quote["id"], XRP_HOT_ACCOUNT)
