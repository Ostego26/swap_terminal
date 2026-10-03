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
from db import SCHEMA, db_session, dict_factory
from services import pricing
from services.pricing import IDS
from services.quote_service import create_quote, measured_or_configured_reserve, own_address_on_chain
from valid_addresses import BTC_REGTEST_DEPOSIT, GRC_PAYOUT
from workers.common import get_config_dict

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
        reserve, how = measured_or_configured_reserve(db, config, {"BTC": adapter}, "BTC", 0.0004)

    assert reserve == pytest.approx(0.0000282), "the measured fee, not the configured constant"
    assert reserve != CONFIGURED
    assert "MEASURED off the BTC chain" in how
    assert str(CONFIGURED) in how, "and the constant it replaced, so the two can be compared"


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
        reserve, how = measured_or_configured_reserve(db, config, {"BTC": BlindAdapter()}, "BTC", 0.0004)

    assert reserve == CONFIGURED
    assert "no BTC adapter in this process can measure a send" in how


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
        reserve, how = measured_or_configured_reserve(db, config, {"BTC": refused}, "BTC", 100.0)

    assert reserve == CONFIGURED
    assert "Method not found" in how, "the daemon's own words, or there is nothing to diagnose"


def test_no_address_of_our_own_falls_back_rather_than_deriving_one(config):
    """getnewaddress would answer in one call and is a WALLET WRITE.

    A priced quote that leaves a key behind would put one in the hot wallet for
    every page refresh, so the quote path must never derive. With no deposit
    address on file for the chain there is nothing to stand in, and the constant is
    what was used before this function existed.
    """
    adapter = MeasuringAdapter()

    with db_session(config["DB_PATH"]) as db:
        reserve, how = measured_or_configured_reserve(db, config, {"BTC": adapter}, "BTC", 0.0004)

    assert reserve == CONFIGURED
    assert "holds no BTC address of its own" in how
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
