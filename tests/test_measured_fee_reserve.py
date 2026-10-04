"""The network fee reserve, measured off the chain for the payout's actual size.

Role: test (seeded database, stub adapters; opens no socket)
Reads: swap_terminal/services/quote_service.py
Writes: a temp database
Can move funds: no
Mainnet-safe: yes

THE OPERATOR AUTHORIZED THIS 2026-10-03 after being shown the numbers, measured on
their node with fundrawtransaction over the real UTXO set:

    BTC   send 0.0004 -> fee 0.00002820      send 2701 -> fee 0.00084240   30x
    LTC   send 1.2    -> fee 0.00010372      send 2701 -> fee 0.00097643   9.4x

A Bitcoin-style fee is bytes times a rate and the bytes are mostly INPUTS, so the
figure moves by an order of magnitude across the payout sizes one desk makes. The
configured constants were wrong in opposite directions the same day -- BTC 41%
low, LTC 4.7x high -- and no single value fixes both.
"""

import sqlite3
import sys
from pathlib import Path
from time import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "swap_terminal"))

from chains.base import RPCAdapter
from config import Config
from db import SCHEMA, db_session, dict_factory
from services import pricing
from services.pricing import IDS
from services.quote_service import (
    RESERVE_FALLBACK,
    RESERVE_MEASURED,
    create_quote,
    measured_or_configured_reserve,
    own_address_on_chain,
)
from valid_addresses import BTC_REGTEST_DEPOSIT, GRC_PAYOUT
from workers.common import get_config_dict

# open_swap.py is at the project ROOT, not under swap_terminal/, so it sorts into its
# own block after the rootless application imports above -- the same split
# tests/test_open_swap.py makes. Asserted on here because the reserve's provenance has
# to be visible on the receipt an operator READS, not only in the service's return
# value (rule 14).
from open_swap import reserve_provenance

CONFIGURED = 0.00002


class MeasuringAdapter:
    """An adapter that answers measure_send_fee, recording the amount it was asked about."""

    def __init__(self, fee=0.0000282, how="fundrawtransaction selected real inputs"):
        self._fee, self._how = fee, how
        self.asked = []

    def measure_send_fee(self, address, amount):
        self.asked.append((address, amount))
        if self._fee is None:
            return None, self._how
        # SCALES WITH THE AMOUNT, like the real thing. A fixed stub fee would let a
        # version that measured ONCE and cached it pass every test here.
        return self._fee * max(amount, 1.0), self._how


class BlindAdapter:
    """An adapter with no measure_send_fee at all -- XRP and SOL, and every stub."""


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "quotes.db"
    connection = sqlite3.connect(path)
    connection.row_factory = dict_factory
    connection.executescript(SCHEMA)
    connection.commit()
    connection.close()

    now = time()
    pricing._cache.update({
        "raw": {cg: {"usd": 100.0, "usd_market_cap": 5_000_000_000.0,
                     "usd_24h_vol": 200_000_000.0, "usd_24h_change": 1.0,
                     "last_updated_at": 1790717713} for cg in IDS.values()},
        "prices": None, "context": None, "source": "seeded",
        "fetched_at": now, "expires_at": now + 999,
    })
    settings = dict(get_config_dict())
    settings["DB_PATH"] = str(path)
    settings["DEFAULT_FEE_BPS"] = 150
    settings["BTC_NETWORK_FEE_RESERVE"] = CONFIGURED
    return settings


def _seed_own_address(config, asset, address):
    """A swap row whose deposit_address is ours on `asset`, which is the stand-in source."""
    with db_session(config["DB_PATH"]) as db:
        db.execute(
            "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, payout_address, "
            "expected_input_amount, quoted_rate, fee_bps, network_fee_reserve, output_amount_estimate, "
            "status, min_confirmations, expires_at, created_at, updated_at) "
            "VALUES (?, 'q_seed', ?, 'GRC', ?, ?, 1.0, 1.0, 150, 0.001, 1.0, 'completed', 2, "
            "'2999-01-01T00:00:00+00:00', '2026-10-01T00:00:00+00:00', "
            "'2026-10-01T00:00:00+00:00')",
            (f"s_seed_{asset}", asset, address, GRC_PAYOUT),
        )
        db.commit()


def test_the_reserve_is_the_MEASURED_fee_when_the_chain_can_be_asked(config):
    """The whole point: the figure comes off the chain for this payout's size."""
    _seed_own_address(config, "BTC", BTC_REGTEST_DEPOSIT)
    adapter = MeasuringAdapter(fee=0.0000282)

    with db_session(config["DB_PATH"]) as db:
        reading = measured_or_configured_reserve(db, config, {"BTC": adapter}, "BTC", 0.0004)

    assert reading.amount == pytest.approx(0.0000282), "the measured fee, not the configured constant"
    assert "MEASURED off the BTC chain" in reading.how
    assert str(CONFIGURED) in reading.how, "and the constant it replaced, so the two can be compared"
    assert reading.provenance == RESERVE_MEASURED


def test_the_measurement_is_asked_about_THIS_payout_and_not_a_fixed_size(config):
    """Because the size is the entire reason a constant does not work.

    A version that measured a nominal amount once -- or cached one answer per chain
    -- would produce a figure as wrong as the constant, for the same reason.
    """
    _seed_own_address(config, "BTC", BTC_REGTEST_DEPOSIT)
    adapter = MeasuringAdapter()

    with db_session(config["DB_PATH"]) as db:
        measured_or_configured_reserve(db, config, {"BTC": adapter}, "BTC", 0.0004)
        measured_or_configured_reserve(db, config, {"BTC": adapter}, "BTC", 2701.0)

    assert [amount for _address, amount in adapter.asked] == [0.0004, 2701.0]


def test_an_adapter_that_cannot_measure_falls_back_and_says_so(config):
    """XRP and SOL price their own fees; so does every existing test stub.

    The fallback is why the whole suite passed unchanged when this landed.
    """
    with db_session(config["DB_PATH"]) as db:
        reading = measured_or_configured_reserve(db, config, {"BTC": BlindAdapter()}, "BTC", 0.0004)

    assert reading.amount == CONFIGURED
    assert "no BTC adapter in this process can measure a send" in reading.how
    assert reading.provenance == RESERVE_FALLBACK
    assert reading.how.startswith(RESERVE_FALLBACK), "the token has to LEAD the sentence, not sit inside it"


def test_a_daemon_without_fundrawtransaction_falls_back_and_carries_its_words(config):
    """THE GRC CASE, AND GRC IS NOT SPECIAL-CASED ANYWHERE.

    Gridcoin is flat -- 8 payouts at exactly 0.00100000, low equal to high, across
    82 to 2701 GRC, which select different input counts -- and its daemon has no
    fundrawtransaction, so the probe returns "Method not found (rpc code -32601)"
    and this falls back to the constant. That is the right answer for a flat chain,
    reached by ASKING rather than by keeping a second table of which chains scale
    (rule 8). A chain that gains the RPC starts being measured with no change here.
    """
    _seed_own_address(config, "BTC", BTC_REGTEST_DEPOSIT)
    refused = MeasuringAdapter(fee=None, how="fundrawtransaction refused (RPCError: Method not found "
                                             "(rpc code -32601))")

    with db_session(config["DB_PATH"]) as db:
        reading = measured_or_configured_reserve(db, config, {"BTC": refused}, "BTC", 100.0)

    assert reading.amount == CONFIGURED
    assert "Method not found" in reading.how, "the daemon's own words, or there is nothing to diagnose"
    assert reading.provenance == RESERVE_FALLBACK


def test_no_address_of_our_own_falls_back_rather_than_deriving_one(config):
    """getnewaddress would answer in one call and is a WALLET WRITE.

    A priced quote that leaves a key behind would put one in the hot wallet for
    every page refresh, so the quote path must never derive. With no deposit
    address on file for the chain there is nothing to stand in, and the constant is
    what was used before this function existed.
    """
    adapter = MeasuringAdapter()

    with db_session(config["DB_PATH"]) as db:
        reading = measured_or_configured_reserve(db, config, {"BTC": adapter}, "BTC", 0.0004)

    assert reading.amount == CONFIGURED
    assert "holds no BTC address of its own" in reading.how
    assert reading.provenance == RESERVE_FALLBACK
    assert adapter.asked == [], "nothing may be measured without a destination to measure against"


def test_the_stand_in_address_is_one_this_desk_derived_for_itself(config):
    """swaps.deposit_address rows are ours: deposit_account() asked this wallet for them.

    Newest first, because an older one may belong to a wallet the operator has
    since repointed, and a stand-in only has to be a valid address of the right
    TYPE.
    """
    _seed_own_address(config, "BTC", BTC_REGTEST_DEPOSIT)

    with db_session(config["DB_PATH"]) as db:
        assert own_address_on_chain(db, "BTC") == BTC_REGTEST_DEPOSIT
        assert own_address_on_chain(db, "LTC") == "", "an address on another chain must not be offered"


def test_the_quote_ROW_carries_the_measured_figure(config):
    """The call site, because the resolver being right is not what has failed before.

    Four times on 2026-10-03 a correct function's CALL SITE discarded its result.
    The reserve on the quote row is what create_swap()'s fee floor and the funding
    gate both read, so this asserts the number that was WRITTEN, not the number the
    resolver returned.
    """
    _seed_own_address(config, "BTC", BTC_REGTEST_DEPOSIT)

    with db_session(config["DB_PATH"]) as db:
        quote = create_quote(db, config, "GRC", "BTC", 10000.0,
                             adapters={"BTC": MeasuringAdapter(fee=0.0000282)})

    assert quote["network_fee_reserve"] != CONFIGURED, (
        "the quote still booked the constant, so the measurement never reached the row"
    )
    assert quote["network_fee_reserve"] > 0


def test_a_quote_with_no_adapters_still_prices(config):
    """The default path, and it must not require a chain to be reachable.

    create_quote(adapters=None) is how every read-only caller prices, and a quote
    that died on a fee probe would be worse than one priced on a constant.
    """
    with db_session(config["DB_PATH"]) as db:
        quote = create_quote(db, config, "GRC", "BTC", 10000.0)

    assert quote["network_fee_reserve"] == CONFIGURED


# --- where the stand-in address comes from, after the first source was wrong ---


class WalletWithAddresses:
    """An adapter whose wallet can be ASKED for an address it already owns."""

    def __init__(self, received=None, labeled=None, fail=()):
        self._received = received if received is not None else [{"address": BTC_REGTEST_DEPOSIT}]
        self._labeled = labeled if labeled is not None else {}
        self._fail = set(fail)
        self.calls = []

    def call(self, method, *params):
        self.calls.append(method)
        if method in self._fail:
            raise RuntimeError(f"{method} not available")
        if method == "listreceivedbyaddress":
            return self._received
        if method == "getaddressesbylabel":
            return self._labeled
        raise AssertionError(f"own_address asked for {method!r}, which this fixture does not stub")


def test_the_wallet_is_asked_BEFORE_the_database(config):
    """THE FIRST SOURCE WAS WRONG FOR THE CASE THAT MATTERS, measured within the hour.

    own_address_on_chain() read swaps.deposit_address, which holds addresses for
    chains used as a SOURCE -- and the reserve is needed for the DESTINATION chain.
    A BTC -> LTC quote fell back to the flat 0.001 constant on the operator's host
    because no swap had ever taken an LTC DEPOSIT: LTC has only ever been paid out
    to. A payout chain that is only ever a destination is the NORMAL case.
    """
    adapter = WalletWithAddresses()
    adapter.own_address = RPCAdapter.own_address.__get__(adapter)

    with db_session(config["DB_PATH"]) as db:
        # NO SWAP ROW AT ALL, which is the state that produced the defect: the
        # database half cannot answer for a chain never used as a source.
        address = own_address_on_chain(db, "LTC", adapter)

    assert address == BTC_REGTEST_DEPOSIT
    assert "listreceivedbyaddress" in adapter.calls, "the wallet has to be asked, not just available"


def test_getaddressesbylabel_is_the_fallback_when_the_first_read_is_absent(config):
    """Daemons differ on which read they expose, so both are tried.

    Neither creates anything -- that is the constraint. getnewaddress would answer
    in one call and DERIVES a key, which a priced quote must never do.
    """
    adapter = WalletWithAddresses(fail={"listreceivedbyaddress"},
                                  labeled={BTC_REGTEST_DEPOSIT: {"purpose": "receive"}})
    adapter.own_address = RPCAdapter.own_address.__get__(adapter)

    assert adapter.own_address() == BTC_REGTEST_DEPOSIT
    assert adapter.calls == ["listreceivedbyaddress", "getaddressesbylabel"]


def test_a_wallet_that_can_answer_neither_read_yields_no_address(config):
    """And the caller then uses the constant and says why. Never a derived key."""
    adapter = WalletWithAddresses(fail={"listreceivedbyaddress", "getaddressesbylabel"})
    adapter.own_address = RPCAdapter.own_address.__get__(adapter)

    assert adapter.own_address() == ""
    assert "getnewaddress" not in adapter.calls, "the quote path must never derive an address"


def test_the_database_still_answers_for_a_wallet_that_cannot(config):
    """The fallback is KEPT rather than deleted: it needs no RPC.

    Its rows are ours by construction -- deposit_account() asked this wallet for
    each one -- so a chain whose daemon exposes neither read is still measured when
    a past deposit gives it an address.
    """
    _seed_own_address(config, "BTC", BTC_REGTEST_DEPOSIT)
    adapter = WalletWithAddresses(fail={"listreceivedbyaddress", "getaddressesbylabel"})
    adapter.own_address = RPCAdapter.own_address.__get__(adapter)

    with db_session(config["DB_PATH"]) as db:
        assert own_address_on_chain(db, "BTC", adapter) == BTC_REGTEST_DEPOSIT


#: THE ONE REAL BTC PAYOUT THIS DESK HAS MADE, and the fee the chain charged for it.
#: payouts.id=21, 0.0040178 BTC, `gettransaction` fee 0.00002820, confirmed on chain,
#: read on the operator's host 2026-10-03. ONE input, which is the cheapest case that
#: exists -- the same wallet measured 0.00084240 at 2701 inputs, 30x.
MEASURED_ONE_INPUT_BTC_FEE = 0.0000282


def test_the_BTC_fallback_reserve_covers_the_only_BTC_fee_ever_charged():
    """The fallback must not sit below a fee this desk has actually paid.

    IT DID, AND THIS IS THE DEFECT THE TEST EXISTS FOR. BTC_NETWORK_FEE_RESERVE was
    0.00002 and the one real BTC payout cost 0.00002820 -- the fallback was 29% BELOW
    what the chain charged, on a ONE-input send. The operator raised it to 0.0000282 on
    2026-10-04: "yes, please resolve this and make it the default for btc".

    WHY BELOW IS THE DANGEROUS DIRECTION AND ABOVE IS MERELY EXPENSIVE. A reserve under
    the real fee prices a quote below what the payout costs, so the payout fails AFTER
    the deposit is irreversible -- which this tree did on 2026-10-03, "Insufficient
    funds (rpc code -4)". A reserve over it under-pays the customer on every quote that
    uses it, which is wrong and is recoverable. So this asserts a FLOOR and not
    equality: a later operator raising it further is not a regression, and the test must
    not force them to come back here to do it.

    THE EQUALITY IS ASSERTED SEPARATELY, BELOW, AND FOR A DIFFERENT REASON -- that the
    figure is the lowest one that clears the floor, which is what makes it not a margin.

    AND THIS IS THE FALLBACK, NOT WHAT MOST QUOTES USE.
    measured_or_configured_reserve() probes the chain for this payout's size and only
    lands here when the probe cannot answer -- measured on the operator's live bitcoind
    2026-10-04 with BTC_RPC_WALLET=desk_hot, 4 of 4 payout sizes (0.0004, 0.0040178,
    0.01, 0.1) came back MEASURED at 0.0000282. Flat because that wallet held exactly
    one utxo, not because the fee does not scale.

    MUTATION: put BTC_NETWORK_FEE_RESERVE back to 0.00002 and this fails with the
    shortfall in the message.
    """
    assert Config.BTC_NETWORK_FEE_RESERVE >= MEASURED_ONE_INPUT_BTC_FEE, (
        f"BTC_NETWORK_FEE_RESERVE is {Config.BTC_NETWORK_FEE_RESERVE} and the only BTC payout this desk "
        f"has made cost {MEASURED_ONE_INPUT_BTC_FEE} (payouts.id=21, gettransaction fee, 1 input). A "
        f"fallback below a fee the chain has charged prices a payout that fails after the deposit is "
        f"irreversible -- which happened on 2026-10-03 as 'Insufficient funds (rpc code -4)'"
    )


def test_the_BTC_fallback_is_the_LOWEST_value_that_clears_the_floor_and_not_a_margin():
    """0.0000282 exactly, because a margin here under-pays every customer who hits it.

    THE TRADE-OFF, AND IT RUNS BOTH WAYS, which is why the number is argued rather than
    rounded up: the worst BTC fee ever measured on this desk is 0.00084240 (2701
    inputs), and using THAT as the fallback would short every customer by 30x the real
    fee, since the reserve is held back out of the gross. So the figure is pinned to the
    measured one-input fee and described as what it is.

    THIS AND THE FLOOR TEST ABOVE SAY DIFFERENT THINGS ON PURPOSE. The floor would pass
    at 0.001; this fails there. Together they say "at least the measured fee, and not a
    satoshi of invented headroom", which is neither assertion alone.

    IT IS THE OPERATOR'S NUMBER AND THIS TEST IS NOT A VETO ON CHANGING IT -- a reserve
    is a pricing decision (rule 16). What it refuses is the number drifting with no
    measurement behind it: anyone raising it has to come here, read the trade-off, and
    write down what they measured.

    MUTATION: 0.00002 fails (29% under the measured fee), and so does 0.0000283.
    """
    assert pytest.approx(MEASURED_ONE_INPUT_BTC_FEE) == Config.BTC_NETWORK_FEE_RESERVE, (
        f"BTC_NETWORK_FEE_RESERVE is {Config.BTC_NETWORK_FEE_RESERVE}, not the measured one-input fee "
        f"{MEASURED_ONE_INPUT_BTC_FEE}. Above it is a margin held back out of every customer's gross; "
        f"below it is a payout that fails after the deposit is irreversible. If this changed "
        f"deliberately, the measurement behind the new figure belongs in config.py beside it"
    )


def test_the_OTHER_FOUR_reserves_are_untouched_by_the_BTC_change():
    """Only BTC was authorized, so only BTC moved.

    The operator's instruction on 2026-10-04 named one asset ("make it the default for
    btc"), and a reserve is a pricing decision per asset (rule 16). LTC in particular is
    ALSO wrong against its measurement -- 0.001 against 0.00010372 at one input, ~10x
    HIGH -- and is deliberately left, because over-reserving under-pays a customer and
    never fails a payout, and nobody asked.

    PINNED SO A LATER PASS CANNOT SWEEP THEM, which is the failure this guards: a reader
    who finds the BTC reasoning convincing is one edit away from "fixing" LTC by the
    same argument, in the opposite direction, unasked.

    MUTATION: change any one of the four and this fails naming it.
    """
    expected = {"LTC": 0.001, "GRC": 0.001, "SOL": 0.000005, "XRP": 0.00001}
    for asset, value in sorted(expected.items()):
        actual = getattr(Config, f"{asset}_NETWORK_FEE_RESERVE")
        assert actual == pytest.approx(value), (
            f"{asset}_NETWORK_FEE_RESERVE is {actual}, not {value}. Only BTC was authorized on "
            f"2026-10-04; changing this one is a pricing decision and the operator's"
        )


def test_a_FALLBACK_and_a_MEASUREMENT_cannot_print_the_same_line(config):
    """The rule 14 half, and after 2026-10-04 the figure alone cannot tell them apart.

    THE TWO CASES BELOW CARRY THE IDENTICAL AMOUNT BY CONSTRUCTION. That is not a
    contrived fixture: BTC_NETWORK_FEE_RESERVE is now 0.0000282 and the measured
    one-input BTC fee IS 0.0000282, so on the operator's own host a fallback and a
    measurement produce the same number. Before 2026-10-04 they differed (2e-05 against
    2.82e-05) and a reader could notice; now nothing in the value distinguishes them,
    and inferring provenance from the figure is rule 17's error.

    WHAT THIS PINS, in order of how silently each could regress:

      the TOKEN differs    provenance is MEASURED or FALLBACK, never prose to parse
      the PROSE differs    and differs in its FIRST WORD, so a fallback is visible on
                           any surface that prints `how` without reading a sentence
      the SENTENCES differ which is the end-to-end property: two readings with the same
                           amount must not render identically

    WHY A TOKEN AT ALL, AND THIS IS MEASURED ON THIS VERY FUNCTION. The measured
    sentence NAMES the configured figure it beat ("... against a configured
    BTC_NETWORK_FEE_RESERVE of 2e-05"), so `"configured" in how.lower()` is True for
    BOTH cases -- somebody checking provenance that way on 2026-10-04 scored every
    MEASURED row as a fallback and reported it. One string cannot be both a human
    sentence and a machine's branch, which is why there are now two values.

    MUTATION: make _fallback() build its sentence without the leading token and the
    startswith assertion fails; make both return the same provenance and the first
    fails; drop the token from the prose only, and the full-line comparison still
    catches it.
    """
    _seed_own_address(config, "BTC", BTC_REGTEST_DEPOSIT)
    config["BTC_NETWORK_FEE_RESERVE"] = MEASURED_ONE_INPUT_BTC_FEE

    with db_session(config["DB_PATH"]) as db:
        measured = measured_or_configured_reserve(
            db, config, {"BTC": MeasuringAdapter(fee=MEASURED_ONE_INPUT_BTC_FEE)}, "BTC", 0.0004)
        fallback = measured_or_configured_reserve(
            db, config, {"BTC": BlindAdapter()}, "BTC", 0.0004)

    assert measured.amount == pytest.approx(fallback.amount), (
        "this test measures nothing unless the two readings carry the SAME amount -- that is the "
        "state the 2026-10-04 constant created and the reason the token exists"
    )
    assert measured.provenance == RESERVE_MEASURED
    assert fallback.provenance == RESERVE_FALLBACK
    assert measured.provenance != fallback.provenance
    assert fallback.how.startswith(RESERVE_FALLBACK), (
        f"a fallback sentence must be LED by the token, so it is visible without reading the "
        f"sentence: {fallback.how!r}"
    )
    assert measured.how.startswith(RESERVE_MEASURED), (
        f"a measured sentence must be led by its token for the same reason: {measured.how!r}"
    )
    assert measured.how != fallback.how, (
        "two readings with the same amount rendered the same sentence, so no surface in the tree can "
        "tell an asked chain from an unasked one"
    )


def test_the_receipt_the_operator_READS_says_which_source_the_reserve_came_from(config):
    """End to end through open_swap.py's real report, not through the service's return.

    THE PROVENANCE REACHED EXACTLY ONE READER BEFORE 2026-10-04: a logger.info inside
    create_quote(). open_swap.py's receipt -- the block the operator pastes back --
    printed `2.82e-05 BTC reserved` and nothing about where that came from, which is
    rule 14's defect on the money path: a reserve that silently came from a constant,
    on a chain whose fee scales 30x with input count, is a payout that can fail after
    the deposit is irreversible.

    WHAT THIS ASSERTS, PRECISELY, BECAUSE AN EARLIER VERSION OF THIS DOCSTRING LIED
    ABOUT IT. It said "ASSERTED ON THE RENDERED LINE, not on the dict" and then called
    open_swap.reserve_provenance() directly -- so deleting `({reserve_provenance(quote)})`
    from report_lines()'s `network fee` line changed what the operator SEES and this file
    stayed green: 17 passed, measured 2026-10-04. A mutation found that; review had
    already read the sentence and believed it.

    So the split is now explicit and both halves exist:

      HERE            the helper's three states, over quotes from the real create_quote()
                      -- that it reports MEASURED, FALLBACK and "not recorded" correctly
      test_open_swap  the RENDERED `--apply` receipt, through the real tool's stdout:
                      test_the_RECEIPT_LINE_says_whether_the_reserve_was_measured_or_fell_back
                      That file owns report_lines() and is where the line can be read
                      off a screen rather than assembled in a test.

    THE THIRD STATE IS ASSERTED TOO and it is the one a reader would most likely get
    wrong. The provenance lives on create_quote()'s RESPONSE and is persisted nowhere
    -- a column on `quotes` is a schema change on the fund path and the operator's
    (rule 16) -- so a quote re-read from SQL has no answer, and the receipt must say
    "not recorded" rather than defaulting to either real state.

    MUTATIONS, 2026-10-04. Defaulting the helper to MEASURED when the key is missing
    fails the third assertion here. Dropping `({reserve_provenance(quote)})` from
    report_lines() SURVIVED this file and is caught by the test_open_swap one -- which
    is why that test exists and why this paragraph names which mutation each half holds.
    """
    _seed_own_address(config, "BTC", BTC_REGTEST_DEPOSIT)

    with db_session(config["DB_PATH"]) as db:
        measured_quote = create_quote(
            db, config, "GRC", "BTC", 10, adapters={"BTC": MeasuringAdapter(fee=MEASURED_ONE_INPUT_BTC_FEE)})
        fallback_quote = create_quote(db, config, "GRC", "BTC", 10, adapters={"BTC": BlindAdapter()})

    swap = {"network_fee_reserve": MEASURED_ONE_INPUT_BTC_FEE}
    assert RESERVE_MEASURED in reserve_provenance(measured_quote), (
        f"the receipt does not say the chain was asked: {reserve_provenance(measured_quote)!r}"
    )
    assert reserve_provenance(fallback_quote).startswith(RESERVE_FALLBACK), (
        f"the receipt does not say the chain was NOT asked: {reserve_provenance(fallback_quote)!r}"
    )
    assert "not recorded" in reserve_provenance({"network_fee_reserve": swap["network_fee_reserve"]}), (
        "a quote with no provenance key must say so, not fall back to claiming a measurement"
    )
