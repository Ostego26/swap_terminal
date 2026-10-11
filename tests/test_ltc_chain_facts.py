"""Rule 11's three per-chain facts for LTC: where each one is, and the one that is a gap.

Role: test (pure; no socket, no subprocess, no chain, no database)
Reads: chains/coin_amounts.py, chains/litecoin.py, config.py,
       modules/htlc_fee.py, services/payout_service.py,
       services/swap_service.py
Writes: a throwaway SQLite database under tmp_path, and nothing else
Can move funds: no. Every adapter here is tests/recording_rpc_adapter.py --
      the REAL RPCAdapter with its transport replaced by a list -- so
      `sendtoaddress` is recorded and never sent. No socket is opened.
Mainnet-safe: yes

=============================================================================
WHY THIS FILE EXISTS: A COMMENT THAT SAID A MEASURED NUMBER WAS UNMEASURED
=============================================================================

chains/litecoin.py is a four-line subclass, and until 2026-10-11 its docstring
said of rule 11's three facts -- decimals, the confirmation requirement, the
dust threshold -- that "they are not here yet", then named the home of one of
them. Two of the three are known. The third, the dust threshold, is MEASURED
against Litecoin Core 0.21.4 in modules/htlc_fee.py and has been since
2026-09-25, and chains/base.RPCAdapter.send_to_address() says the opposite in
as many words: the dust limit "depends on the output type, and no part of this
tree measures it yet".

So a reader deciding whether the empty subclass matters was told the number did
not exist. It does, and on LTC it is TEN TIMES Bitcoin's, which is the whole
reason it is worth finding.

THE DOCSTRING IS NOT WHAT THIS FILE ASSERTS ON, and that is deliberate. A test
that greps prose is a detector that reads its own search string, which is a
mistake this session's HANDOFF.md section 6 records being made four times in
one day. What this file does instead is pin the NUMBERS the docstring cites --
each one read out of the function that computes it -- so that if any of them
moves, the suite fails and whoever moves it is sent to the paragraph that
quotes it.

=============================================================================
THE LAST TEST IN THIS FILE ASSERTS A DEFECT, ON PURPOSE
=============================================================================

test_a_sub_dust_LTC_payout_is_still_SENT_rather_than_refused measures the gap
rather than a fix: a 2000-base-unit LTC payout, below the 2940 the table gives
for a `tltc1q...` output and above Bitcoin's 294, passes every gate in the
brokered payout path and reaches `sendtoaddress`.

Adding the refusal is fund movement -- it decides whether a swap may proceed at
all -- so it is the operator's (rule 16) and is reported rather than made. The
assertion is written the way tests/test_htlc_locktime_encoding.py's DEFECT
assertions were before that fix landed: it states what is true today, says in
its own body that it is a defect, and is INVERTED by the change that closes it.
A gap with no test is a sentence in a report that ages out; a gap with a test
is a line somebody has to delete on purpose.
"""

from __future__ import annotations

import pytest
from chains.coin_amounts import CHAIN_DECIMALS
from chains.litecoin import LitecoinAdapter
from config import Config
from db import SCHEMA, apply_migrations, connect_db
from modules.htlc_fee import DUST_RELAY_FEE_SAT_PER_KVB, dust_threshold_satoshis
from recording_rpc_adapter import RecordingRPCAdapter
from services import payout_service
from services.swap_service import get_min_confirmations
from valid_addresses import GRC_DESK_DEPOSIT, LTC_PARTICIPANT

CONFIG = {name: getattr(Config, name) for name in dir(Config) if name.isupper()}

# THE FIVE SCRIPT SHAPES, BUILT FROM BYTES rather than derived from an address,
# because dust_threshold_satoshis() prices the scriptPubKey's LENGTH and the
# input that would spend it -- so what matters here is the shape, and spelling
# the bytes is what makes the shape visible to a reader checking the table.
# The 20- and 32-byte payloads are filler: no threshold depends on their value.
P2PKH_SCRIPT = bytes([0x76, 0xA9, 0x14]) + b"\x11" * 20 + bytes([0x88, 0xAC])
P2SH_SCRIPT = bytes([0xA9, 0x14]) + b"\x22" * 20 + bytes([0x87])
P2WPKH_SCRIPT = bytes([0x00, 0x14]) + b"\x33" * 20
P2WSH_SCRIPT = bytes([0x00, 0x20]) + b"\x44" * 32
P2TR_SCRIPT = bytes([0x51, 0x20]) + b"\x55" * 32

#: EXACTLY THE TABLE chains/litecoin.py's docstring PRINTS, as (shape, BTC, LTC).
#: Copied from that table on purpose: the point is that the two agree, and the
#: assertion below is what makes the copy safe rather than rule 8's bug with a
#: delay on it -- a table spelled twice is fine only when a test fails the
#: moment they disagree.
DUST_TABLE = (
    ("P2WPKH", P2WPKH_SCRIPT, 294, 2940),
    ("P2WSH", P2WSH_SCRIPT, 330, 3300),
    ("P2TR", P2TR_SCRIPT, 330, 3300),
    ("P2SH", P2SH_SCRIPT, 540, 5400),
    ("P2PKH", P2PKH_SCRIPT, 546, 5460),
)

#: The payout this desk would make that is the defect's exact shape: ABOVE
#: Bitcoin's P2WPKH floor of 294 base units and BELOW Litecoin's 2940. Spelled
#: as a coin amount because that is how every amount in this tree is spelled,
#: which is the reason the two chains look the same at a glance.
SUB_DUST_LTC = 0.00002
SUB_DUST_BASE_UNITS = 2000


def test_LTC_decimals_are_8_and_the_payout_path_already_reads_them():
    """Fact one: NOT a gap. The number is in chains/coin_amounts.py.

    Asserted through CHAIN_DECIMALS rather than against a literal 8 alone,
    because the claim in chains/litecoin.py's docstring is about WHERE the
    number lives, not only what it is: if that table stopped carrying LTC,
    fit_to_chain_precision() would return the amount unchanged with a sentence
    saying so, and an over-precise payout would reach `sendtoaddress` again.
    """
    assert CHAIN_DECIMALS["LTC"] == 8, (
        f"LTC decimals are {CHAIN_DECIMALS.get('LTC')!r} in chains/coin_amounts.CHAIN_DECIMALS, "
        f"not 8. chains/litecoin.py's docstring cites 8 and RPCAdapter.send_to_address() fits "
        f"every payout to it"
    )
    assert CHAIN_DECIMALS["LTC"] == CHAIN_DECIMALS["BTC"], (
        "the Bitcoin-derived chains are cited as sharing one precision; they no longer do"
    )


def test_the_LTC_confirmation_requirement_is_2_and_comes_from_Config():
    """Fact two: NOT a gap. The number is Config.LTC_MIN_CONFIRMATIONS.

    Read through get_min_confirmations(), which is the function that actually
    writes `swaps.min_confirmations`, rather than off Config directly -- the
    docstring's claim is about the ROUTE, and since 2026-10-11 that route also
    passes through chains/registry.validate_min_confirmations(). A threshold
    that Config carried but the chokepoint dropped or rewrote would satisfy a
    test that read Config alone.
    """
    assert Config.LTC_MIN_CONFIRMATIONS == 2, (
        f"Config.LTC_MIN_CONFIRMATIONS is {Config.LTC_MIN_CONFIRMATIONS}, not the 2 that "
        f"chains/litecoin.py's docstring cites"
    )
    assert get_min_confirmations(CONFIG, "LTC") == 2, (
        "the threshold Config carries is not the one that reaches a swap row"
    )


def test_the_adapter_subclass_really_carries_NOTHING_but_the_asset():
    """The premise of the whole docstring: the subclass is four lines.

    If somebody adds a per-chain fact to this class, the paragraph that says
    "NONE of the three is here" becomes the wrong comment all over again -- so
    this is the test that sends them to it. It asserts on the class's OWN
    namespace, not on `dir()`, because everything inherited from RPCAdapter is
    exactly what the docstring says it is relying on.
    """
    own = sorted(name for name in vars(LitecoinAdapter) if not name.startswith("__"))
    assert own == ["asset"], (
        f"LitecoinAdapter now defines {own}. chains/litecoin.py's docstring states that none of "
        f"rule 11's three facts is in this subclass; adding one means rewriting that paragraph "
        f"in the same commit (rule 16: a wrong comment is a bug)"
    )
    assert LitecoinAdapter.asset == "LTC"
    # Confirmed rather than trusted, because the whole LTC payout path rests on
    # it: litecoind signs its own sends, so no arming overlay exists for this
    # chain and nothing would report a missing one.
    assert LitecoinAdapter.can_spend is True, (
        "LTC is payable because the daemon's own wallet signs; a False here would silently "
        "remove LTC from services/payout_service.payable_assets()"
    )
    assert LitecoinAdapter.payout_refusal == "", (
        "payout_refusal is the sentence shown beside a DISABLED pair, and it must be empty "
        "while can_spend is True"
    )


@pytest.mark.parametrize(("shape", "script", "btc", "ltc"), DUST_TABLE)
def test_the_dust_table_in_the_docstring_is_what_the_code_computes(shape, script, btc, ltc):
    """Fact three, half one: the figures chains/litecoin.py prints are real.

    MUTATION: change any LTC figure in that docstring's table and this test
    still passes -- which is correct and is the point of not grepping prose.
    What it catches is the other direction: a change to
    DUST_RELAY_FEE_SAT_PER_KVB or to dust_threshold_satoshis() that leaves the
    docstring quoting numbers no node applies. MUTATE
    DUST_RELAY_FEE_SAT_PER_KVB["LTC"] from 30000 to 3000 and every LTC row
    here fails by exactly the factor the docstring calls out.
    """
    assert dust_threshold_satoshis("BTC", script) == btc, (
        f"BTC {shape} dust is {dust_threshold_satoshis('BTC', script)}, not the {btc} "
        f"chains/litecoin.py's table prints"
    )
    assert dust_threshold_satoshis("LTC", script) == ltc, (
        f"LTC {shape} dust is {dust_threshold_satoshis('LTC', script)}, not the {ltc} "
        f"chains/litecoin.py's table prints. That table is what tells a reader the LTC floor "
        f"is measured at all; it must not quote a figure the code has moved away from"
    )


def test_LTC_dust_is_TEN_TIMES_BITCOINS_which_is_the_whole_finding():
    """Fact three, half two: the RATIO, because it is what makes the band exist.

    If the two rates ever converge, the "a payout fine on BTC is refused on LTC"
    paragraph in chains/litecoin.py stops being true and has to go. Asserted on
    the rate AND on a threshold, because the rate alone would pass if
    dust_threshold_satoshis() stopped reading the table.
    """
    assert DUST_RELAY_FEE_SAT_PER_KVB["LTC"] == 10 * DUST_RELAY_FEE_SAT_PER_KVB["BTC"], (
        f"LTC's dust relay rate is {DUST_RELAY_FEE_SAT_PER_KVB['LTC']} against BTC's "
        f"{DUST_RELAY_FEE_SAT_PER_KVB['BTC']}; chains/litecoin.py's docstring says ten times"
    )
    btc_floor = dust_threshold_satoshis("BTC", P2WPKH_SCRIPT)
    ltc_floor = dust_threshold_satoshis("LTC", P2WPKH_SCRIPT)
    assert btc_floor < SUB_DUST_BASE_UNITS < ltc_floor, (
        f"{SUB_DUST_BASE_UNITS} base units is supposed to sit INSIDE the band -- deliverable on "
        f"BTC at {btc_floor}, refused on LTC at {ltc_floor}. Without that it is not the fixture "
        f"the next test needs"
    )


def _seeded_sub_dust_swap(tmp_path):
    """One GRC -> LTC swap already credited and waiting to pay out 0.00002 LTC.

    SEEDED PAST THE DEPOSIT ON PURPOSE. The defect is about the ORDER of events:
    the customer's deposit is credited and irreversible before the payout is
    attempted, so a refusal that only happens at send time strands a paid-for
    swap. `credited_at` and `actual_input_amount` are set for that reason, not
    for convenience -- services/payout_service.payout_amount() reads both.
    """
    conn = connect_db(str(tmp_path / "ltc_sub_dust.db"), create=True)
    conn.executescript(SCHEMA)
    apply_migrations(conn)
    conn.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps,"
        " network_fee_reserve, output_amount_estimate, expires_at, created_at)"
        " VALUES ('q_dust','GRC','LTC',1.0,1.0,150,0.001,?,"
        "'2999-01-01T00:00:00+00:00','2026-10-11T00:00:00+00:00')",
        (SUB_DUST_LTC,),
    )
    conn.execute(
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, payout_address,"
        " expected_input_amount, actual_input_amount, credited_at, quoted_rate, fee_bps,"
        " network_fee_reserve, output_amount_estimate, status, min_confirmations,"
        " expires_at, created_at, updated_at)"
        " VALUES ('s_dust','q_dust','GRC','LTC',?,?,1.0,1.0,'2026-10-11T00:00:00+00:00',1.0,150,"
        "0.001,?,'payout_pending',?,'2999-01-01T00:00:00+00:00',"
        "'2026-10-11T00:00:00+00:00','2026-10-11T00:00:00+00:00')",
        (GRC_DESK_DEPOSIT, LTC_PARTICIPANT, SUB_DUST_LTC, Config.GRC_MIN_CONFIRMATIONS),
    )
    conn.commit()
    return conn


def test_a_sub_dust_LTC_payout_is_still_SENT_rather_than_refused(tmp_path):
    """A DEFECT ASSERTION. It states what is true on 2026-10-11, not what should be.

    This is the gap chains/litecoin.py's docstring names: 2000 base units -- below
    Litecoin Core 0.21.4's 2940 for the `tltc1q...` output this swap pays, above
    Bitcoin's 294 -- passes every gate in the brokered payout path and reaches
    `sendtoaddress`.

    The adapter is tests/recording_rpc_adapter.RecordingRPCAdapter, which IS the
    production RPCAdapter with its transport replaced by a list, so
    fit_to_chain_precision() and the `fitted <= 0` refusal are the real ones and
    the amount asserted on is the amount that would have gone on the wire.

    WHEN THE REFUSAL LANDS, THIS TEST IS INVERTED rather than deleted: `sent`
    becomes `[]`, the swap's status becomes whatever the operator chose for a
    payout refused before any network call, and the assertions below say so.
    The DEFECT comment goes with it. Leaving it passing after a fix would be a
    test pinning behavior that has been replaced rather than moved, which is
    rule 9's list of things to cull.

    WHAT IS NOT MEASURED HERE (rule 17): that litecoind refuses 2000 base units.
    No litecoind is reachable from this container. The floor is Core's own
    GetDustThreshold arithmetic over modules/htlc_fee's measured relay rate, and
    the test above pins that half. This half measures only that OUR path does
    not refuse first, which is the half that is ours to change.
    """
    conn = _seeded_sub_dust_swap(tmp_path)
    adapter = RecordingRPCAdapter("LTC")

    payout_service.process_pending_payouts(conn, CONFIG, {"LTC": adapter})

    base_units = [round(amount * 10 ** CHAIN_DECIMALS["LTC"]) for amount in adapter.sent]
    assert base_units == [SUB_DUST_BASE_UNITS], (
        f"DEFECT ASSERTION, and it has stopped holding: {base_units} base units reached "
        f"`sendtoaddress` rather than the {[SUB_DUST_BASE_UNITS]} this file measured on "
        f"2026-10-11. If a dust refusal has landed, invert this test -- `sent` should now be "
        f"[] and the swap should not be `completed` -- and delete the DEFECT wording from "
        f"this file and from chains/litecoin.py's docstring in the same commit (rule 19)"
    )
    row = conn.execute("SELECT status, payout_txid FROM swaps WHERE id = 's_dust'").fetchone()
    assert row["status"] == "completed", (
        f"the sub-dust swap is {row['status']!r} rather than `completed`; a refusal has been "
        f"added somewhere on this path and this DEFECT assertion needs inverting"
    )
    assert row["payout_txid"], (
        "a txid was recorded for a payout no node would relay, which is the half of this that "
        "makes it worse than a plain failure: the database says the customer was paid"
    )
