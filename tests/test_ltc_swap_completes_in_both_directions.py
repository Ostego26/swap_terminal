"""An LTC swap completes in BOTH directions, driven through the real services.

Role: test (end to end over the real deposit and payout services)
Reads: chains/litecoin.py, chains/base.py, config.py, db.py,
       services/deposit_service.py, services/payout_service.py
Writes: a throwaway SQLite database under tmp_path, and nothing else
Can move funds: no. Both adapters subclass the REAL RPCAdapter and replace
      `call` with a seeded script, so `sendtoaddress` is recorded rather than
      sent. No socket is opened and no subprocess is started.
Mainnet-safe: yes

=============================================================================
WHY A WHOLE-PATH TEST FOR THIS ONE CHAIN
=============================================================================

Every piece of the LTC path already has a unit test. What none of them answers
is the question the operator actually asks -- "does an LTC swap complete" --
and the three times this tree has shipped a stalled swap, every piece passed
and the ASSEMBLY did not:

  2026-10-03  the deposit was flawless, 2 of 2 confirmations, credited and
              irreversible, and the payout died 11 seconds later on
              "Insufficient funds (rpc code -4)". Every gate passed; nobody
              had asked whether the money was there.
  2026-10-04  every BTC and LTC deposit that confirmed between two 15s polls
              fabricated an event at vout 0, because `getrawtransaction` with
              no block hash cannot see a confirmed transaction on a node with
              no -txindex. Nothing raised.
  2026-10-11  ICP_MIN_CONFIRMATIONS=2 held every ICP deposit in `confirming`
              forever, found by running the real process_active_swaps() forty
              times rather than by reading the gate.

So this file runs services/deposit_service.process_active_swaps() and
services/payout_service.process_pending_payouts() for real, in a loop, over
seeded swaps in a real schema, and asserts on the ROWS and on the RPCs -- which
is what CLAUDE.md's "verify by behavior, never by reading the code" asks for at
the one altitude the unit tests cannot reach.

=============================================================================
WHAT THE DAEMON STUB IS, AND WHY IT ANSWERS IN LITECOIN CORE 0.21.4's SHAPE
=============================================================================

The operator's litecoind is a real TESTNET node on 19332 answering as Litecoin
Core 0.21.4, which is four years behind Bitcoin Core and reports
`scriptPubKey.addresses` (plural) with no `address` key, and which cannot
answer `getrawtransaction` for a CONFIRMED transaction without -txindex. Both
of those are seeded here, because both are states that have silently produced
wrong rows on this path before.

NOT MEASURED, AND SAID SO (rule 17): no litecoind is reachable from this
container -- no daemon is installed, docker is unusable and no socket can be
opened -- so nothing here establishes what the real daemon answers. It
establishes what THIS code does with the answers the operator measured off
theirs on 2026-09-25 and 2026-10-04.
"""

from __future__ import annotations

from typing import NamedTuple

import pytest
from chains.base import RPCError
from chains.bitcoin import BitcoinAdapter
from chains.litecoin import LitecoinAdapter
from config import Config
from db import SCHEMA, apply_migrations, connect_db
from services import deposit_service, payout_service
from valid_addresses import (
    BTC_REGTEST_DEPOSIT,
    BTC_REGTEST_SOMEBODY_ELSE,
    LTC_PARTICIPANT,
    LTC_REFUND,
)

CONFIG = {name: getattr(Config, name) for name in dir(Config) if name.isupper()}

#: The operator's own LTC figure, from the swap measured on their host
#: 2026-10-10 and recorded in swap_terminal/deposit_payers.py: "a swap wanted
#: 0.06030068 LTC". A real amount rather than a round one, because the deposit
#: match is `abs(value - amount) < SATOSHI` and the payout is scaled by
#: actual/expected -- a round number cannot show a rounding defect in either.
LTC_AMOUNT = 0.06030068
#: The payout each direction quotes. Both are far above every dust floor in
#: modules/htlc_fee.DUST_RELAY_FEE_SAT_PER_KVB, deliberately: the sub-dust case
#: is its own measurement in tests/test_ltc_chain_facts.py and mixing it in
#: here would make a completed swap and a dust-bound one share one fixture.
#: 0.0004 BTC is the operator's own smallest real payout size (config.py's
#: measured-reserve table lists it) rather than a round figure.
LTC_PAYOUT_AMOUNT = 0.05
BTC_PAYOUT_AMOUNT = 0.0004

#: The deposit pays somebody else at vout 0 and the swap at vout 1. NOT 0,
#: because deposit_vout_artifact.SUSPECT_VOUT is 0 and an event there cannot be
#: told from a fabricated one by looking at the row.
DEPOSIT_VOUT = 1
LTC_DEPOSIT_TXID = "cd" * 32
LTC_PAYOUT_TXID = "ltc-payout-txid-from-the-stub"
BTC_PAYOUT_TXID = "btc-payout-txid-from-the-stub"
CONFIRMATIONS = 3


class SeededDaemon:
    """Shared transport stand-in: `call` records and answers from a script.

    ONE class for both chains rather than two, because the only thing that
    differs is the asset label and which methods each daemon has -- and two
    near-copies of a stub is the same rule 8 shape as two near-copies of a
    client. The per-chain answers live in the subclasses below.
    """

    def __init__(self, **connection):
        self.calls: list[tuple] = []
        super().__init__(**connection)

    def sent(self) -> list[tuple]:
        """Every `sendtoaddress` that reached the transport, as (address, amount).

        A LIST, so "nothing was sent" is an assertion on `== []` rather than on
        the absence of an exception -- the distinction rule 13 draws about
        stops, which this directory's XRP tests already make about calls that
        must not happen.
        """
        return [params for method, params in self.calls if method == "sendtoaddress"]


class Litecoind(SeededDaemon, LitecoinAdapter):
    """The real LitecoinAdapter, answering as Litecoin Core 0.21.4 on testnet.

    `-txindex` is absent, which is the operator's measured node state and the
    one that used to fabricate: `getrawtransaction` raises -5 for the confirmed
    deposit and the wallet route reads the outputs instead.
    """

    def __init__(self):
        # Not credentials; `call` is overridden and opens no socket. The port is
        # the operator's real litecoind testnet port, which
        # network_target.CHAIN_PORTS classifies as TEST.
        super().__init__(
            user="fixture-rpc-user",
            password="fixture-rpc-auth-value",  # noqa: S106 -- checked: not a secret and never used; `call` is overridden below.
            host="127.0.0.1",
            port=19332,
        )

    def call(self, method, *params) -> object:
        self.calls.append((method, params))
        if method == "listtransactions":
            return [{"category": "receive", "address": LTC_PARTICIPANT,
                     "amount": LTC_AMOUNT, "txid": LTC_DEPOSIT_TXID,
                     "confirmations": CONFIRMATIONS}]
        if method == "getrawtransaction":
            raise RPCError(
                "No such mempool transaction. Use -txindex or provide a block hash to enable "
                "blockchain transaction queries. Use gettransaction for wallet transactions. "
                "(rpc code -5)"
            )
        if method == "gettransaction":
            return {"confirmations": CONFIRMATIONS, "hex": "0100" + "00" * 32}
        if method == "decoderawtransaction":
            # LITECOIN CORE 0.21.4's KEY SET: `addresses` plural, `reqSigs`, no
            # `address`. Seeded this way on purpose -- a fixture carrying both
            # spellings would pass whether or not script_pub_key.py reads the
            # plural one.
            return {"vout": [
                {"n": 0, "value": 1.0, "scriptPubKey": {
                    "addresses": [LTC_REFUND], "asm": "", "hex": "0014" + "11" * 20,
                    "reqSigs": 1, "type": "witness_v0_keyhash"}},
                {"n": DEPOSIT_VOUT, "value": LTC_AMOUNT, "scriptPubKey": {
                    "addresses": [LTC_PARTICIPANT], "asm": "", "hex": "0014" + "22" * 20,
                    "reqSigs": 1, "type": "witness_v0_keyhash"}},
            ]}
        if method in ("validateaddress", "getaddressinfo"):
            # A real wallet answers `ismine`, and answering a stub txid instead
            # would make every ownership question read "not established" --
            # which is not what a wallet does, and is what
            # tests/recording_rpc_adapter.py records turning six correct guards
            # into red tests.
            return {"isvalid": True, "address": params[0], "ismine": False}
        if method == "getbalance":
            return 5.0
        if method == "sendtoaddress":
            return LTC_PAYOUT_TXID
        raise AssertionError(f"the litecoind stub has no scripted answer for {method}")


class Bitcoind(SeededDaemon, BitcoinAdapter):
    """The real BitcoinAdapter, for the other end of each pair.

    BITCOIN AND NOT GRIDCOIN ON THE OTHER END, and that is a correction rather
    than a preference. The first draft paired LTC -> GRC, and the GRC payout
    DEFERRED on every cycle -- "GRC payouts need the wallet fully unlocked, and
    GRIDCOIN_WALLET_PASSPHRASE is not set in this process's environment" -- so
    the swap sat in `payout_pending` and the test reported a stall in the LTC
    deposit leg that was really the Gridcoin wallet lock one chain over. A
    fixture whose failure names the wrong chain is worse than no fixture.

    BTC <-> LTC also has the better provenance: both daemons sign their own
    sends with no arming step and no passphrase, and config.py records a real
    BTC -> LTC swap priced on the operator's host on 2026-10-05. The Gridcoin
    deferral has its own file in tests/test_gridcoin_wallet_lock.py.

    It never detects a deposit here: in the BTC -> LTC direction the swap is
    seeded past its deposit on purpose, so the LTC half of that direction is
    measured on the PAYOUT and nothing else.
    """

    def __init__(self):
        super().__init__(
            user="fixture-rpc-user",
            password="fixture-rpc-auth-value",  # noqa: S106 -- checked: as above.
            host="127.0.0.1",
            port=18332,
        )

    def call(self, method, *params) -> object:
        self.calls.append((method, params))
        if method == "listtransactions":
            return []
        if method in ("validateaddress", "getaddressinfo"):
            return {"isvalid": True, "address": params[0], "ismine": False}
        if method == "getbalance":
            return 10.0
        if method == "sendtoaddress":
            return BTC_PAYOUT_TXID
        raise AssertionError(f"the bitcoind stub has no scripted answer for {method}")


class SeededSwap(NamedTuple):
    """The one swap a test seeds, as a record rather than as nine arguments.

    AN OBJECT HERE AND NOT ON THE FUND PATH, and the difference is worth one
    sentence because this tree's noqa comments argue the opposite case half a
    dozen times -- "bundling them into an object would add a type without
    removing a parameter". That is true where each value is an independent fact
    the caller computed separately. These nine are not: they are ONE swap and
    its quote, every field of the two rows that any test here varies, and the
    helper's job is to write that record to disk. ruff's PLR0913 fired at
    9 > 5 and rule 12 says to answer a complexity finding by extracting rather
    than by suppressing, so the record is extracted.

    `created_at` and `expires_at` are not fields: every test here wants a swap
    created in the past and expiring far in the future, and a test that wanted
    otherwise would be about the deadline rather than about LTC.
    """

    swap_id: str
    from_asset: str
    to_asset: str
    deposit_address: str
    payout_address: str
    input_amount: float
    output_amount: float
    status: str


def _seed(conn, swap: SeededSwap):
    """One quote and one swap, written with the real schema's own columns.

    min_confirmations comes from CONFIG rather than from the record, because
    that is what services/swap_service.get_min_confirmations() would have
    written: a test that could choose it would be able to seed a threshold the
    real creation path refuses.
    """
    conn.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps,"
        " network_fee_reserve, output_amount_estimate, expires_at, created_at)"
        " VALUES (?,?,?,?,1.0,150,0.001,?,"
        "'2999-01-01T00:00:00+00:00','2026-10-11T00:00:00+00:00')",
        (f"q_{swap.swap_id}", swap.from_asset, swap.to_asset, swap.input_amount,
         swap.output_amount),
    )
    conn.execute(
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, payout_address,"
        " expected_input_amount, quoted_rate, fee_bps, network_fee_reserve,"
        " output_amount_estimate, status, min_confirmations, expires_at, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,1.0,150,0.001,?,?,?,"
        "'2999-01-01T00:00:00+00:00','2026-10-11T00:00:00+00:00','2026-10-11T00:00:00+00:00')",
        (swap.swap_id, f"q_{swap.swap_id}", swap.from_asset, swap.to_asset,
         swap.deposit_address, swap.payout_address, swap.input_amount, swap.output_amount,
         swap.status, CONFIG[f"{swap.from_asset}_MIN_CONFIRMATIONS"]),
    )
    conn.commit()


@pytest.fixture
def conn(tmp_path):
    connection = connect_db(str(tmp_path / "ltc_both_directions.db"), create=True)
    connection.executescript(SCHEMA)
    apply_migrations(connection)
    return connection


def _drive(conn, adapters, cycles=4):
    """The two real worker cycles, several times over.

    MORE THAN ONCE ON PURPOSE. A swap crosses three statuses on its way out and
    each worker advances it by one, so a single pass cannot tell "completed"
    from "stuck one step short" -- which is exactly the shape of the ICP defect
    found on 2026-10-11 by running forty cycles. Four is enough for this path
    and a stall would show as the status it stopped at rather than as a hang.
    """
    for _cycle in range(cycles):
        deposit_service.process_active_swaps(conn, CONFIG, adapters)
        payout_service.process_pending_payouts(conn, CONFIG, adapters)


def test_LTC_AS_THE_SOURCE_is_detected_credited_and_handed_to_the_payout(conn):
    """LTC -> BTC. The deposit leg: detection, vout, confirmations, the credit.

    MUTATION: in chains/base._extract_matching_vouts(), replace
    `pays_address(script_pub_key, address)` with the Core-22-only
    `script_pub_key.get("address")` spelling, and the deposit_events row comes
    back at vout 0 from the FABRICATED branch -- the state every LTC deposit on
    a Core-0.21.4-shaped daemon would be in. Run and confirmed 2026-10-11.
    """
    _seed(conn, SeededSwap(
        swap_id="s_ltc_source", from_asset="LTC", to_asset="BTC",
        deposit_address=LTC_PARTICIPANT, payout_address=BTC_REGTEST_DEPOSIT,
        input_amount=LTC_AMOUNT, output_amount=BTC_PAYOUT_AMOUNT,
        status="awaiting_deposit",
    ))
    litecoind, bitcoind = Litecoind(), Bitcoind()

    _drive(conn, {"LTC": litecoind, "BTC": bitcoind})

    swap = conn.execute("SELECT * FROM swaps WHERE id = 's_ltc_source'").fetchone()
    assert swap["actual_input_amount"] == LTC_AMOUNT, (
        f"the credited LTC amount is {swap['actual_input_amount']} rather than the "
        f"{LTC_AMOUNT} the deposit output carries"
    )
    assert swap["credited_at"], (
        f"the LTC deposit was never credited; the swap is {swap['status']!r} with "
        f"failed_reason {swap['failed_reason']!r}. LTC_MIN_CONFIRMATIONS is "
        f"{CONFIG['LTC_MIN_CONFIRMATIONS']} and the deposit was seeded at {CONFIRMATIONS}"
    )
    assert swap["status"] == "completed", (
        f"the LTC -> BTC swap stopped at {swap['status']!r} ({swap['failed_reason']!r}) rather "
        f"than completing. A status short of `completed` with nothing in failed_reason is the "
        f"stall shape: credited, recorded, and not moving"
    )

    events = conn.execute(
        "SELECT * FROM deposit_events WHERE swap_id = 's_ltc_source'").fetchall()
    assert len(events) == 1, (
        f"{len(events)} deposit_events rows for one payment. TWO is the artifact measured on "
        f"2026-10-03: upsert_deposit_event() keys on (asset, txid, vout), so a real row and a "
        f"fabricated vout=0 row coexist and refresh_swap_from_chain() sums both"
    )
    assert events[0]["vout"] == DEPOSIT_VOUT, (
        f"the LTC deposit was recorded at vout {events[0]['vout']} rather than "
        f"{DEPOSIT_VOUT}. vout 0 is what the fabricated branch stamps"
    )
    assert events[0]["asset"] == "LTC"


def test_LTC_AS_THE_DESTINATION_is_paid_and_the_txid_is_recorded(conn):
    """BTC -> LTC. The payout leg: the amount, the send, the recorded txid.

    Seeded at `payout_pending` with the deposit already credited, because the
    LTC half of this direction is the payout and nothing else -- driving the BTC
    deposit as well would make this test's result depend on
    BTC_MIN_CONFIRMATIONS and on bitcoind's own vout matching, which has its
    own file in tests/test_deposit_vout_matching.py.

    MUTATION: in chains/base.send_to_address(), return "" instead of the result
    of `self.call("sendtoaddress", ...)`. The send still happens and
    `swaps.payout_txid` comes back empty -- a payment nobody can reconcile
    against the chain, which a retry would then send twice. Run and confirmed
    2026-10-11.

    A MUTATION THAT WAS TRIED AND DOES **NOT** FAIL THIS TEST, recorded because
    the first draft of this docstring asserted that it would: giving
    LitecoinAdapter `can_spend = False` leaves this test GREEN. The payout still
    goes out. That is not a defect -- `can_spend` is read by
    chains/registry.why_cannot_pay_out(), whose callers are
    services/swap_service.create_swap() and the pair and admin views, so it
    gates whether such a swap can be CREATED and not whether an existing one
    pays. process_pending_payouts() never asks. Written down because predicting
    a mutation's effect and then not running it is the exact failure HANDOFF.md
    section 6 catalogues four times in one day, and because the next reader
    needs to know where that flag does and does not bite.
    tests/test_ltc_chain_facts.py is where the flag's value is actually pinned.
    """
    _seed(conn, SeededSwap(
        swap_id="s_ltc_destination", from_asset="BTC", to_asset="LTC",
        deposit_address=BTC_REGTEST_SOMEBODY_ELSE, payout_address=LTC_REFUND,
        input_amount=1.0, output_amount=LTC_PAYOUT_AMOUNT, status="payout_pending",
    ))
    conn.execute(
        "UPDATE swaps SET actual_input_amount = 1.0, credited_at = ? WHERE id = 's_ltc_destination'",
        ("2026-10-11T00:00:00+00:00",),
    )
    conn.commit()
    litecoind, bitcoind = Litecoind(), Bitcoind()

    _drive(conn, {"LTC": litecoind, "BTC": bitcoind})

    assert litecoind.sent() == [(LTC_REFUND, LTC_PAYOUT_AMOUNT)], (
        f"litecoind was asked to send {litecoind.sent()} rather than "
        f"[({LTC_REFUND!r}, {LTC_PAYOUT_AMOUNT})]. An empty list means nothing was sent at "
        f"all, which is what an unpayable adapter looks like"
    )
    swap = conn.execute("SELECT * FROM swaps WHERE id = 's_ltc_destination'").fetchone()
    assert swap["status"] == "completed", (
        f"the BTC -> LTC swap is {swap['status']!r} ({swap['failed_reason']!r})"
    )
    assert swap["payout_txid"] == LTC_PAYOUT_TXID, (
        f"swaps.payout_txid is {swap['payout_txid']!r} rather than the txid litecoind "
        f"returned. A payout whose txid was not recorded is a payment nobody can reconcile "
        f"against the chain, and a retry would send it twice"
    )

    payouts = conn.execute(
        "SELECT * FROM payouts WHERE swap_id = 's_ltc_destination'").fetchall()
    assert len(payouts) == 1, (
        f"{len(payouts)} payouts rows for one swap. TWO is the double-send measured before "
        f"idx_payouts_one_live_per_swap existed"
    )
    assert payouts[0]["asset"] == "LTC"
    assert payouts[0]["txid"] == LTC_PAYOUT_TXID
    assert payouts[0]["status"] == "broadcast"
    assert payouts[0]["destination_address"] == LTC_REFUND, (
        f"the payout went to {payouts[0]['destination_address']!r} rather than the swap's own "
        f"payout_address"
    )


def test_the_LTC_payout_amount_is_fitted_to_eight_decimals_before_the_send(conn):
    """The one arithmetic that has already broken a real BTC/LTC payout.

    Measured 2026-10-03: a quoted payout is a float with as many decimals as
    the arithmetic produced, and Bitcoin Core's ParseFixedPoint(value, 8)
    REJECTS more than eight rather than rounding -- so an over-precise amount
    fails with "Invalid amount" AFTER the customer's deposit is irreversible.
    Gridcoin masked it by rounding; Litecoin does not.

    The payout here is scaled by actual/expected, which is what MAKES the extra
    decimals: 0.05 * (0.98765432 / 1.0) is 0.049382716 to nine places. The
    assertion is on the base-unit count of what reached the transport, so it
    cannot be satisfied by a value that merely looks short.

    MUTATION, AND THE RESULT IS THE INTERESTING PART. Three were run on
    2026-10-11 and only the third fails this test:

      chains/base.send_to_address() sends `float(amount)` instead of `fitted`
          GREEN. The amount arriving is already quantized.
      services/payout_service.amount_decided_and_logged() takes `amount`
      instead of quantize_for_chain()'s result
          GREEN. The adapter quantizes it again.
      BOTH of the above at once
          RED: 0.049382716 LTC reaches `sendtoaddress`, which is 4938271.6
          base units -- not a whole number, so litecoind answers `Invalid
          amount` (rpc code -3) with the customer's deposit already credited.

    So this is a test of a PAIR of guards rather than of one, and that is what
    the code says it is: payout_service's own comment reads "the adapter
    quantizes AGAIN after this function has". Written down rather than quietly
    labeled with the first mutation that came to mind, because a mutation
    nobody ran is a reason to believe and not a measurement (rule 17) -- and a
    reader deleting one of the two sites as redundant needs to know that no
    test will stop them.
    """
    _seed(conn, SeededSwap(
        swap_id="s_ltc_precision", from_asset="BTC", to_asset="LTC",
        deposit_address=BTC_REGTEST_SOMEBODY_ELSE, payout_address=LTC_REFUND,
        input_amount=1.0, output_amount=LTC_PAYOUT_AMOUNT, status="payout_pending",
    ))
    conn.execute(
        "UPDATE swaps SET actual_input_amount = 0.98765432, credited_at = ?"
        " WHERE id = 's_ltc_precision'",
        ("2026-10-11T00:00:00+00:00",),
    )
    conn.commit()
    litecoind = Litecoind()

    _drive(conn, {"LTC": litecoind, "BTC": Bitcoind()})

    assert len(litecoind.sent()) == 1, f"one send expected, got {litecoind.sent()}"
    _address, amount = litecoind.sent()[0]
    base_units = amount * 10 ** 8
    assert base_units == pytest.approx(round(base_units), abs=1e-6), (
        f"{amount!r} LTC is {base_units!r} base units, which is not a whole number -- so the "
        f"amount carries more than eight decimals and litecoind would answer `Invalid amount` "
        f"(rpc code -3) with the customer's deposit already credited"
    )
    assert amount <= LTC_PAYOUT_AMOUNT * 0.98765432, (
        f"the fit rounded UP to {amount!r}. chains/coin_amounts.fit_to_chain_precision() is "
        f"DOWN, never nearest: rounding up sends a fraction more than was quoted out of the "
        f"hot wallet on every payout"
    )


def test_LTC_has_all_ten_of_its_directions_in_ALLOWED_PAIRS():
    """Five out and five in, against every other asset this terminal trades.

    A direction missing here is not a broken swap, it is a swap that cannot be
    STARTED -- services/quote_service.validate_pair() refuses with "Unsupported
    trading pair" and the ATM page shows the pair disabled. Asserted as the
    full set rather than as a count, because a count of ten could be made up by
    the wrong ten.
    """
    traded = {asset for pair in Config.ALLOWED_PAIRS for asset in pair}
    others = traded - {"LTC"}

    out_of_ltc = {to for frm, to in Config.ALLOWED_PAIRS if frm == "LTC"}
    into_ltc = {frm for frm, to in Config.ALLOWED_PAIRS if to == "LTC"}

    assert out_of_ltc == others, (
        f"LTC can be swapped OUT to {sorted(out_of_ltc)}; the terminal trades "
        f"{sorted(others)} besides LTC. Missing: {sorted(others - out_of_ltc)}"
    )
    assert into_ltc == others, (
        f"LTC can be swapped INTO from {sorted(into_ltc)}; missing: "
        f"{sorted(others - into_ltc)}"
    )
    assert len(out_of_ltc) + len(into_ltc) == 10, (
        f"the terminal trades {len(traded)} assets, so LTC has {len(others)} directions each "
        f"way. If that is no longer ten, this assertion is the one to update -- and the two "
        f"above are the ones that matter"
    )
