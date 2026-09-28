"""The burn guards, asserted on what they DID with seeded inputs -- never on source text.

Role: test (read-only against the tree; writes only throwaway SQLite under tmp_path)
Reads: modules/address_authority.py, services/payout_service.py, services/swap_service.py,
       services/swap_view.py, modules/htlc_fee.py
Writes: a disposable database per test
Can move funds: no. Every adapter here is a stub and no socket is opened.
Mainnet-safe: yes

WHAT THIS FILE IS FOR, 2026-09-27. Operator: "obviously spin up an agent and make this burn
proof", then "i mean, make the receive path burn proof too".

`modules/address_network.is_valid_address()` arrived in f805efa and nothing on the fund path
called it. An undecodable address reached a real transaction and the money was burned --
unspendable by anybody, not stolen, gone.

EVERY ASSERTION HERE IS ON BEHAVIOR. No test in this file reads a docstring, greps a source
file, or matches SQL text. That restriction is not stylistic: on 2026-09-27 three tests
elsewhere in this project matched docstring prose and PASSED while the code beneath them was
mutated, and a fourth asserted four private keys were absent from an attribute that does not
exist -- so it compared them against the empty string and would have passed with a debug line
echoing every key. The only evidence that counts is "seeding condition X produced or
suppressed outcome Y".

Every guard below was MUTATION-CHECKED against a verified-green baseline (1652 tests, 0
failures, 0 errors, 0 skipped): the guard was removed or inverted, the suite was run, and the
tests named in each docstring's MUTATION line went red. A guard whose removal kills no test
is not a guard.

NO ADDRESS LITERAL APPEARS IN THIS FILE. Valid ones are derived from phrases in
tests/valid_addresses.py; invalid ones are derived FROM valid ones via INVALID_PLACEHOLDERS.
tests/test_address_literals_are_valid.py is a clean gate with no baseline and would fail on
either.
"""

from __future__ import annotations

import base58
import bech32
import pytest
from chains.registry import build_adapters
from config import Config
from conftest import RPC_FIXTURE_AUTH
from db import SCHEMA, apply_migrations, connect_db
from modules.address_authority import (
    INVALID,
    NO_VALIDATOR,
    NOT_EXPRESSED,
    UNDETERMINED,
    VALID,
    VALIDATORS,
    check_address,
    check_receive_address,
    expected_network,
)
from modules.address_network import (
    BASE58_VERSION_ASSETS,
    BECH32_HRPS,
    BECH32_HRPS_BY_ASSET,
    MAINNET,
    P2PKH_VERSIONS,
    P2SH_VERSIONS,
    TESTNET,
    is_valid_address,
)
from modules.htlc_fee import usable_platform_fee_address
from services.payout_service import process_pending_payouts
from services.swap_service import DEPOSIT_TAG_COLUMN, create_swap
from services.swap_view import deposit_instruction
from valid_addresses import (
    BTC_PARTICIPANT,
    GRC_PAYOUT,
    INVALID_PLACEHOLDERS,
    LTC_P2SH_SCRIPT_ADDRESS2,
    LTC_PARTICIPANT,
    LTC_REGTEST_DEPOSIT,
    SOL_PAYOUT,
    XMR_PAYOUT,
    XRP_HOT_ACCOUNT,
)

# A MAINNET Gridcoin address, DERIVED here rather than written down, and it is the one
# address in this repository that is deliberately not on a test network.
#
# It has to exist, because the accident this whole day is about is a mainnet address
# arriving where a testnet one was expected: `gridcoinresearchd getnewaddress` with no
# `-testnet` put a real mainnet address into the operator's live staking wallet on
# 2026-09-27. A receive-path test that cannot produce one cannot prove the network check
# works, and would pass just as well with the check deleted.
#
# Derived from the version byte in modules/address_network.BASE58_VERSION_ASSETS over a
# fixed non-key payload, so nobody holds a key for it and no literal can be mistyped into
# the tree. tests/test_address_literals_are_valid.py sees no literal because there is none.
GRC_MAINNET_NOBODY_HOLDS = base58.b58encode_check(bytes([0x3E]) + bytes(range(20))).decode()

# A LEGACY-FORM P2SH ADDRESS -- version byte 0x05, the `3...` form -- likewise derived and
# likewise mainnet, for the one case where AMBIGUITY is the correct answer.
#
# ADDED AFTER A MUTATION SURVIVED. Changing BASE58_VERSION_ASSET[0x05] from None to "BTC"
# killed no test, which means nothing in this suite exercised the byte that two chains share on
# MAINNET -- and writing the test then found a second defect the mutant had not: the table's
# one-chain-or-None shape could not say "these two and not that one", so the GRC assertion
# below FAILED on first run. A `3...` Bitcoin P2SH address was being accepted as a Gridcoin
# payout destination. The table is a frozenset per byte now, which is the actual fix. Measured from each project's own chainparams.cpp on 2026-09-27 rather than
# recalled: bitcoin/src/kernel/chainparams.cpp:177 and litecoin/src/chainparams.cpp:146 BOTH
# declare SCRIPT_ADDRESS = 5. So a `3...` address is genuinely either chain's, and the mutant
# would have refused every legacy-form Litecoin P2SH payout -- a false refusal on real money,
# which is the failure this whole module is built to avoid.
P2SH_LEGACY_SHARED_BY_BTC_AND_LTC = base58.b58encode_check(bytes([0x05]) + bytes(range(20))).decode()

CONFIG_TESTNET_GRC = {
    "GRC_MIN_CONFIRMATIONS": 6,
    "LTC_MIN_CONFIRMATIONS": 2,
    # 25715 is a Gridcoin TEST port per network_target.CHAIN_PORTS, which is what makes
    # expected_network() say "testnet" without this test asserting that string itself.
    "RPC": {"GRC": {"port": 25715}},
}


# ---------------------------------------------------------------------------
# THE CENTRAL TRAP: why this is a table and not one call
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("asset", "address"),
    [("XMR", XMR_PAYOUT), ("SOL", SOL_PAYOUT)],
)
def test_the_blanket_guard_would_have_refused_monero_and_solana(asset, address):
    """THE MEASUREMENT THAT DECIDED THE DESIGN, kept as a test so nobody re-walks into it.

    The obvious fix for a burn is one line at the send site:

        if not is_valid_address(address): refuse

    This asserts that line would have refused a VALID payout on two live chains.
    `is_valid_address()` understands bech32, Bitcoin-alphabet base58check and XRP-alphabet
    base58check; Monero is a different base58 with a Keccak checksum and Solana is a bare
    32-byte key with no checksum at all. Config.ALLOWED_PAIRS carries ("GRC","XMR"), so a
    Monero payout is not hypothetical -- and a false refusal there lands on a swap whose
    deposit is ALREADY OURS and already credited.

    Both halves are asserted together on purpose: the first alone would read as a complaint
    about address_network, and the second alone would read as a nice property of the
    authority. Together they are the reason the authority exists.

    MUTATION: point VALIDATORS["XMR"] (or ["SOL"]) at the bitcoin-family validator and the
    second assertion fails.
    """
    assert is_valid_address(address) is False, (
        f"address_network.is_valid_address() now accepts {asset}; if that is deliberate, the "
        f"argument in modules/address_authority.py's header has changed and must be rewritten"
    )
    assert check_address(asset, address).state == VALID


def test_every_asset_this_terminal_can_reach_has_a_validator():
    """THE CLEAN GATE THAT LETS NO_VALIDATOR PASS THROUGH SAFELY AT RUNTIME.

    modules/address_authority.check_address() returns NO_VALIDATOR for an unknown asset, and
    every fund-path caller lets that THROUGH with a warning rather than refusing -- because
    refusing a chain we cannot check is the false-refusal outage the test above measures,
    arriving by the door a future chain comes in by.

    That would be a hole big enough to drive the original defect through if nothing closed
    it. This closes it: a chain that can be constructed (chains/registry) or swapped
    (Config.ALLOWED_PAIRS) must have a validator, or the suite fails. So NO_VALIDATOR is
    unreachable today -- measured, not assumed -- and a new chain arrives as a red test
    rather than as an unchecked payout.

    Derived from the registry and the pair table rather than listed here, so adding a chain
    cannot leave this assertion describing the old set (rule 11). The reachable set comes out
    of build_adapters() itself, given a fully-configured RPC mapping -- not out of a list
    written here, and not out of a private constant: a chain that can be CONSTRUCTED is
    exactly a chain whose addresses can reach the fund path, and that is the function that
    decides it.

    MUTATION: delete VALIDATORS["XMR"] and this fails naming XMR.
    """
    fully_configured = {
        "BTC": {"user": "u", "password": RPC_FIXTURE_AUTH, "host": "127.0.0.1", "port": 18443},
        "LTC": {"user": "u", "password": RPC_FIXTURE_AUTH, "host": "127.0.0.1", "port": 19443},
        "GRC": {"user": "u", "password": RPC_FIXTURE_AUTH, "host": "127.0.0.1", "port": 25715},
        "SOL": {"url": "http://127.0.0.1:8899"},
        "XRP": {"url": "http://127.0.0.1:5005"},
        "XMR": {"host": "127.0.0.1", "port": 18081},
    }
    # build_adapters() opens no socket -- chains/registry.py's header states it and its own
    # tests hold it -- so this constructs six adapters and touches nothing.
    reachable = set(build_adapters(fully_configured))
    swappable = {asset for pair in Config.ALLOWED_PAIRS for asset in pair}

    missing = sorted((reachable | swappable) - set(VALIDATORS))

    assert missing == [], (
        f"{missing} can be reached or swapped by this terminal and has no entry in "
        f"modules/address_authority.VALIDATORS, so its addresses go unchecked onto the fund path"
    )


# ---------------------------------------------------------------------------
# the vocabulary, and that it cannot drift (rule 8)
# ---------------------------------------------------------------------------


def test_the_version_asset_table_covers_exactly_the_version_network_tables():
    """One vocabulary, two readings, and neither may grow a byte the other lacks.

    P2PKH_VERSIONS and P2SH_VERSIONS say which NETWORK a version byte belongs to;
    BASE58_VERSION_ASSETS says which CHAINS it can belong to. A byte in one and not the other is rule
    8's shape exactly -- a new chain added to the network tables would silently get no
    cross-chain protection, and a byte added here alone would refuse an address the decoder
    calls valid.

    MUTATION: add or remove any key in BASE58_VERSION_ASSETS and this fails, naming the side.
    """
    networks = set(P2PKH_VERSIONS) | set(P2SH_VERSIONS)

    assert set(BASE58_VERSION_ASSETS) == networks, (
        f"only in BASE58_VERSION_ASSETS: {sorted(set(BASE58_VERSION_ASSETS) - networks)}; "
        f"only in the network tables: {sorted(networks - set(BASE58_VERSION_ASSETS))}"
    )
    # And no entry may be EMPTY. A byte mapped to no chain at all would refuse every address
    # carrying it, which is the false-refusal outage expressed as a typo in a table.
    empty = sorted(f"{byte:#04x}" for byte, chains in BASE58_VERSION_ASSETS.items() if not chains)
    assert empty == [], f"these version bytes belong to no chain, so every address using them is refused: {empty}"


def test_the_flat_hrp_table_is_derived_from_the_per_asset_one():
    """BECH32_HRPS is built from BECH32_HRPS_BY_ASSET, so it cannot disagree with it.

    Asserted on the VALUES rather than trusted from the comprehension, because the failure
    being prevented is somebody later "fixing" one table by hand.

    MUTATION: respell BECH32_HRPS as a literal missing "bcrt" and this fails.
    """
    derived = {hrp: net for table in BECH32_HRPS_BY_ASSET.values() for hrp, net in table.items()}

    assert derived == BECH32_HRPS
    # And GRC is PRESENT with an empty table, not absent: "Gridcoin has no bech32" has to be
    # readable off the vocabulary, which is what lets _bitcoin_bech32() say so.
    assert BECH32_HRPS_BY_ASSET["GRC"] == {}


# ---------------------------------------------------------------------------
# what each validator accepts and refuses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "asset", "address"),
    [
        ("a Litecoin bech32 address paid from a Bitcoin wallet", "BTC", LTC_PARTICIPANT),
        ("a Bitcoin bech32 address paid from a Litecoin wallet", "LTC", BTC_PARTICIPANT),
        ("an XRP classic account paid as Bitcoin", "BTC", XRP_HOT_ACCOUNT),
        ("a Monero address paid as Bitcoin", "BTC", XMR_PAYOUT),
        ("a Solana key paid as Bitcoin", "BTC", SOL_PAYOUT),
        ("a bech32 address paid as Gridcoin, which has no bech32", "GRC", BTC_PARTICIPANT),
        ("a Bitcoin address paid as Monero", "XMR", BTC_PARTICIPANT),
        ("a Gridcoin address paid as XRP", "XRP", GRC_PAYOUT),
        # ADDED AFTER A MUTATION SURVIVED. Removing the version-byte cross-chain check in
        # _bitcoin_base58() killed NO test, because every case above is bech32 or a different
        # encoding entirely -- the base58 half of the check was unguarded. These two are
        # mainnet base58 addresses of the wrong chain, which is the only input that exercises
        # it: a GRC mainnet 0x3E address and a Litecoin SCRIPT_ADDRESS2 address, both real,
        # both well-formed, both a burn if paid as Bitcoin.
        ("a Gridcoin MAINNET address paid as Bitcoin", "BTC", GRC_MAINNET_NOBODY_HOLDS),
        ("a Litecoin P2SH address paid as Bitcoin", "BTC", LTC_P2SH_SCRIPT_ADDRESS2),
    ],
)
def test_a_real_address_on_the_wrong_chain_is_refused(label, asset, address):
    """EVERY ONE OF THESE IS A VALID ADDRESS. That is what makes them dangerous.

    A typo gets caught by a checksum. `ltc1...` in a Bitcoin payout field does not: it
    decodes, its checksum holds, it is mainnet, and modules/address_network's blanket
    decoder says yes. The money is exactly as gone as if the string had been noise.

    This is the tightening that makes the authority more than a per-asset wrapper, and it is
    the only assertion set here that a "does it decode" check could not make.

    MUTATION: have VALIDATORS return address_network.decodes_as_address() and six of these
    eight go green-to-red the other way -- they start returning VALID.
    """
    verdict = check_address(asset, address)

    assert verdict.state == INVALID, f"{label}: accepted as a {asset} address ({verdict.why})"
    # Rule 14: the refusal has to be readable, and the address is what an operator matches
    # against the screen in front of them.
    assert address in verdict.why


def test_the_legacy_p2sh_byte_is_accepted_by_both_chains_that_declare_it():
    """MAINNET AMBIGUITY, WHICH IS AS REAL AS THE TESTNET KIND AND WAS UNTESTED.

    Bitcoin and Litecoin both declare SCRIPT_ADDRESS = 5 -- read off their own
    chainparams.cpp files on 2026-09-27, not recalled -- so a `3...` P2SH address belongs to
    either. Litecoin additionally has SCRIPT_ADDRESS2 = 50, which is what litecoind ENCODES
    with, but the legacy form still DECODES and still receives money.

    Naming BTC for 0x05 would refuse every legacy-form Litecoin P2SH payout. A mutation that
    did exactly that survived the whole suite before this test existed, which is the only
    reason this test is here and is worth saying: the gap was found by mutating, not by
    reading.

    MUTATION: set BASE58_VERSION_ASSETS[0x05] = frozenset({"BTC"}) and the LTC assertion
    fails; frozenset({"LTC"}) and the BTC one does; add "GRC" and the last one does.
    """
    assert check_address("BTC", P2SH_LEGACY_SHARED_BY_BTC_AND_LTC).state == VALID
    assert check_address("LTC", P2SH_LEGACY_SHARED_BY_BTC_AND_LTC).state == VALID
    # And it is still refused for Gridcoin, which declares SCRIPT_ADDRESS = 85 and not 5.
    assert check_address("GRC", P2SH_LEGACY_SHARED_BY_BTC_AND_LTC).state == INVALID


def test_a_testnet_address_is_not_attributed_to_one_of_the_three_chains_that_share_its_byte():
    """THE LIMIT, ASSERTED SO NOBODY TIGHTENS PAST IT.

    BTC, LTC and GRC testnet all use version byte 0x6F, which modules/address_network.py's
    header states. So a testnet base58 address CANNOT be attributed to one of the three by
    decoding, and a validator that claimed otherwise would refuse every valid Bitcoin
    testnet address in this repository -- the false-refusal outage again, from the opposite
    direction to the Monero one.

    GRC_PAYOUT is a 0x6F address and it must be VALID for BTC, LTC and GRC alike.

    MUTATION: map 0x6F to frozenset({"GRC"}) in BASE58_VERSION_ASSETS and the first two fail.
    """
    assert check_address("BTC", GRC_PAYOUT).state == VALID
    assert check_address("LTC", GRC_PAYOUT).state == VALID
    assert check_address("GRC", GRC_PAYOUT).state == VALID


@pytest.mark.parametrize("failure_mode", sorted(INVALID_PLACEHOLDERS))
def test_every_known_malformation_is_refused(failure_mode):
    """The six failure modes tests/valid_addresses.py derives, each against GRC.

    Derived from a valid address in every case, so what is being tested is the CHECK and not
    the author's ability to type a broken string.
    """
    verdict = check_address("GRC", INVALID_PLACEHOLDERS[failure_mode])

    assert verdict.state == INVALID, f"{failure_mode} was accepted: {verdict.why}"


@pytest.mark.parametrize("empty", ["", "   ", None, 0])
def test_an_absent_address_is_invalid_and_says_what_it_got(empty):
    """`swaps.payout_address` is nullable, so a row read back with NULL reaches a validator.

    INVALID rather than an exception: a guard that raises on a bad row takes the whole
    payout worker down with it and the other pending swaps never get paid.
    """
    verdict = check_address("BTC", empty)

    assert verdict.state == INVALID
    assert repr(empty) in verdict.why


def test_an_asset_with_no_validator_is_neither_valid_nor_invalid():
    """The third state, and that it says what to do about itself (rule 14).

    Asserted as "not VALID and not INVALID" rather than "== NO_VALIDATOR" alone, because the
    hazard is a caller reading it as either one.
    """
    verdict = check_address("DOGE", GRC_PAYOUT)

    assert verdict.state == NO_VALIDATOR
    assert verdict.state not in (VALID, INVALID)
    assert verdict.refuses is False, "NO_VALIDATOR must never be read as a refusal"
    assert "DOGE" in verdict.why


def test_a_solana_address_reports_that_it_carries_no_checksum():
    """Rule 14: VALID means something weaker on Solana and the operator is told so.

    A Solana address is base58 of 32 bytes with NO checksum, so "valid" cannot mean "not a
    typo" -- only "not the wrong length". Printing the same bare word for this as for a
    checksummed Bitcoin address would overstate what was checked.

    MUTATION: drop the NOTE clause from _solana() and this fails while every state assertion
    in the file still passes -- which is the point of asserting on the message at all.
    """
    verdict = check_address("SOL", SOL_PAYOUT)

    assert verdict.state == VALID
    assert "NO CHECKSUM" in verdict.why
    assert verdict.network == NOT_EXPRESSED


def test_a_truncated_solana_key_is_the_one_malformation_solana_can_detect():
    """And the limit is real: length is all there is. Derived, not written."""
    assert check_address("SOL", SOL_PAYOUT[:20]).state == INVALID


# ---------------------------------------------------------------------------
# UNDETERMINED: an address we cannot place is not an address we refuse
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "address"),
    [
        # A bech32 checksum that HOLDS under an hrp no table here knows. `doge1...` stands in
        # for whatever the next chain is; `rltc1...` was this case until 2026-09-27, and is
        # now a known hrp -- which is why the stand-in is needed to keep the branch covered.
        ("valid bech32 under an unknown hrp", bech32.encode("doge", 0, bytes(range(20)))),
        # A 21-byte base58check payload whose checksum holds, under a version byte no table
        # here knows. 0x7B is arbitrary and unassigned in this repository; 0x3A was this case
        # until 2026-09-27 and is now known.
        ("valid base58check under an unknown version byte",
         base58.b58encode_check(bytes([0x7B]) + bytes(range(20))).decode()),
    ],
)
def test_a_well_formed_address_this_repository_cannot_place_is_not_refused(label, address):
    """THE LESSON FROM THE LIVE REGTEST RUN, GENERALIZED PAST THE TWO ENTRIES THAT WERE ADDED.

    On 2026-09-27 the operator's BTC->LTC regtest swap produced two addresses this module
    refused: a `rltc1...` bech32 (Litecoin's regtest hrp, missing from BECH32_HRPS) and a
    `Q...` base58 (version 0x3A, Litecoin's second P2SH byte, missing from P2SH_VERSIONS).
    Both came off running daemons. Both are valid. Both read INVALID.

    Adding the two entries fixes those two strings and NOTHING ELSE. A missing table entry
    and a missing validator produce the identical outcome -- a false refusal on a real
    customer payout -- and this state is what stops the third gap from being an outage.

    The rule: REFUSE ONLY WHAT IS ACTIVELY CONTRADICTED. A checksum that holds is evidence the
    string is an address; "I have never seen this version byte" is a gap in our tables, not
    evidence against it (rule 17).

    MUTATION: return INVALID from either unknown-vocabulary branch and this goes red, while
    every refusal test in this file stays green -- which is the asymmetry the state exists for.
    """
    verdict = check_address("BTC", address)

    assert verdict.state == UNDETERMINED, f"{label}: {verdict.state} -- {verdict.why}"
    assert verdict.refuses is False, f"{label}: a valid address we cannot place was REFUSED"
    assert verdict.unchecked is True, "rule 14: the caller has to be able to say nothing was checked"


def test_a_string_that_decodes_as_nothing_is_still_refused():
    """The line UNDETERMINED must not blur, or the burn guard is gone.

    A broken checksum is not "I could not place it" -- it is "nothing on any chain can spend
    this". Asserted beside the test above because the two together are the whole distinction.
    """
    for label, value in INVALID_PLACEHOLDERS.items():
        verdict = check_address("BTC", value)
        assert verdict.state == INVALID, f"{label} became {verdict.state}: {verdict.why}"


def test_the_two_litecoin_formats_from_the_live_run_are_now_placed_not_merely_tolerated():
    """The entries were ADDED, so these are VALID rather than UNDETERMINED.

    Tolerating them would have been the lazy half of the fix: an UNDETERMINED verdict carries
    no network, so the receive-path check could not tell a regtest deposit address from a
    mainnet one -- and that is the accident this whole day is about.

    MUTATION: remove `rltc` from BECH32_HRPS_BY_ASSET["LTC"] and the first assertion drops to
    UNDETERMINED; remove 0x3A from BASE58_VERSION_ASSETS and the second does.
    """
    assert check_address("LTC", LTC_REGTEST_DEPOSIT).state == VALID
    assert check_address("LTC", LTC_REGTEST_DEPOSIT).network == TESTNET
    assert check_address("LTC", LTC_P2SH_SCRIPT_ADDRESS2).state == VALID
    assert check_address("LTC", LTC_P2SH_SCRIPT_ADDRESS2).network == TESTNET


def test_an_unplaceable_payout_is_sent_and_says_it_was_not_checked(tmp_path, caplog):
    """THE PAYOUT PATH'S ANSWER TO UNDETERMINED, which is the same as its answer to
    NO_VALIDATOR: send, and say loudly that nothing was verified.

    This is the branch the live regtest run would have hit. A Litecoin regtest payout address
    that the tables could not place must NOT be refused -- the swap's deposit is already ours
    and already credited, so a refusal strands the customer to protect us from a risk we
    cannot even measure.

    MUTATION: have payout_service refuse on `verdict.unchecked` and the sends assertion fails.
    """
    adapter = RecordingAdapter()
    unplaceable = base58.b58encode_check(bytes([0x7B]) + bytes(range(20))).decode()
    conn = _seed_pending(tmp_path / "unplaceable.db", to_asset="LTC", payout_address=unplaceable)

    with caplog.at_level("WARNING"):
        process_pending_payouts(conn, {}, {"LTC": adapter})

    assert adapter.sends == [(unplaceable, 0.0975)]
    assert "NOT CHECKED" in caplog.text
    assert UNDETERMINED in caplog.text, "rule 14: WHICH kind of unchecked decides the fix"


# ---------------------------------------------------------------------------
# the NETWORK half, which only the receive path asks
# ---------------------------------------------------------------------------


def test_a_mainnet_address_is_refused_when_this_process_is_configured_for_testnet():
    """THE 2026-09-27 ACCIDENT, ASSERTED.

    `gridcoinresearchd getnewaddress` with no `-testnet` produced a MAINNET address in the
    operator's live staking wallet. Handed to a customer by a terminal that believes it is
    on testnet, that is real money paid onto a chain nothing here is watching.

    The address decodes perfectly, so check_address() alone says VALID -- asserted first,
    because that is what makes the network half load-bearing rather than decorative.

    MUTATION: delete the network comparison in check_receive_address() and the second
    assertion fails while the first still passes.
    """
    assert check_address("GRC", GRC_MAINNET_NOBODY_HOLDS).state == VALID

    verdict = check_receive_address("GRC", GRC_MAINNET_NOBODY_HOLDS, TESTNET)

    assert verdict.state == INVALID
    assert verdict.network == MAINNET
    assert GRC_MAINNET_NOBODY_HOLDS in verdict.why


def test_the_same_address_passes_when_this_process_is_configured_for_mainnet():
    """The other direction, so the check cannot pass by refusing mainnet unconditionally."""
    assert check_receive_address("GRC", GRC_MAINNET_NOBODY_HOLDS, MAINNET).state == VALID
    assert check_receive_address("GRC", GRC_PAYOUT, TESTNET).state == VALID


def test_an_unestablished_network_does_not_refuse_anything():
    """Rule 17: "I could not tell" and "it is wrong" must never be the same value.

    network_target.classify() returns UNRECOGNIZED for a daemon on a custom -rpcport, which
    is an ordinary configuration. Turning that into a refusal would break every operator who
    runs a daemon anywhere but the conventional port.

    MUTATION: make expected_network() fall back to TESTNET for UNRECOGNIZED and the mainnet
    assertion below fails.
    """
    assert check_receive_address("GRC", GRC_MAINNET_NOBODY_HOLDS, None).state == VALID
    assert expected_network("GRC", {"GRC": {"port": 24601}}) is None, "a custom port is not a network claim"
    assert expected_network("GRC", {}) is None, "nothing configured is not a network claim"
    assert expected_network("GRC", {"GRC": {"port": 25715}}) == TESTNET
    assert expected_network("GRC", {"GRC": {"port": 15715}}) == MAINNET


def test_a_chain_whose_addresses_carry_no_network_says_the_check_was_skipped():
    """XRP. The ledger has no testnet address format -- the network is the server you submit
    to -- so there is nothing to compare. Reported rather than passed silently: an operator
    who asked for a network check is entitled to know it did not happen here.

    MUTATION: return `verdict` unchanged for NOT_EXPRESSED and the message assertion fails.
    """
    verdict = check_receive_address("XRP", XRP_HOT_ACCOUNT, TESTNET)

    assert verdict.state == VALID
    assert "NETWORK NOT CHECKED" in verdict.why


def test_xrp_xmr_and_sol_have_no_port_convention_to_compare_against():
    """And that is stated rather than silently absent: those three are not in
    network_target.CHAIN_PORTS, which that module's own comment explains."""
    for asset in ("XRP", "XMR", "SOL"):
        assert expected_network(asset, {asset: {"port": 18081, "url": "http://x"}}) is None


# ---------------------------------------------------------------------------
# THE SEND PATH: services/payout_service.py, where customer money leaves
# ---------------------------------------------------------------------------


class RecordingAdapter:
    """A payout adapter that records sends instead of making them. Opens no socket."""

    can_spend = True
    payout_refusal = ""

    def __init__(self):
        self.sends = []

    def send_to_address(self, address, amount):
        self.sends.append((address, float(amount)))
        return f"stub-txid-{len(self.sends)}"

    def get_balance(self):
        return 100.0

    def call(self, method, *params):
        return {}


def _seed_pending(db_path, *, to_asset, payout_address):
    """One swap in `payout_pending` -- the only state process_pending_payouts() acts on."""
    conn = connect_db(str(db_path))
    conn.executescript(SCHEMA)
    now = "2026-09-27T00:00:00+00:00"
    conn.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps, "
        "network_fee_reserve, output_amount_estimate, expires_at, created_at) "
        "VALUES ('q1','GRC',?,100.0,0.001,150,0.001,0.0975,?,?)",
        (to_asset, now, now),
    )
    conn.execute(
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, payout_address, "
        "expected_input_amount, actual_input_amount, quoted_rate, fee_bps, network_fee_reserve, "
        "output_amount_estimate, status, min_confirmations, created_at, updated_at, credited_at, expires_at) "
        "VALUES ('s1','q1','GRC',?,?,?,100.0,100.0,0.001,150,0.001,0.0975,'payout_pending',6,?,?,?,?)",
        (to_asset, GRC_PAYOUT, payout_address, now, now, now, now),
    )
    conn.execute(
        "INSERT INTO wallet_inventory (asset, hot_confirmed, hot_reserved, hot_available, updated_at) "
        "VALUES (?, 10.0, 0.0, 10.0, ?)",
        (to_asset, now),
    )
    conn.commit()
    apply_migrations(conn)
    return conn


def test_an_undecodable_payout_address_is_never_sent(tmp_path, caplog):
    """THE GUARD. The next fund-path statement after it is adapter.send_to_address().

    `sends == []` is the assertion that matters and it is asserted FIRST: everything else
    here is about how the refusal was recorded, and a refusal that still sent the money
    would be worthless however well it was recorded.

    MUTATION: delete the `if verdict.refuses:` block in process_pending_payouts() and this
    goes red on the first assertion, plus the three below it.
    """
    adapter = RecordingAdapter()
    conn = _seed_pending(tmp_path / "burn.db", to_asset="LTC",
                         payout_address=INVALID_PLACEHOLDERS["bech32 checksum"])

    with caplog.at_level("ERROR"):
        process_pending_payouts(conn, {}, {"LTC": adapter})

    assert adapter.sends == [], f"money was sent to an undecodable address: {adapter.sends}"
    # Rule 14: the refusal names the address AND the reason, on the screen the operator is
    # actually watching -- swaps.failed_reason reached nothing they were looking at.
    assert "REFUSED BEFORE SENDING" in caplog.text
    assert INVALID_PLACEHOLDERS["bech32 checksum"] in caplog.text
    assert "s1" in caplog.text


def test_a_refused_payout_leaves_no_reserved_inventory_and_no_payouts_row(tmp_path):
    """THE TRANSACTION BOUNDARY, which is the part a careless guard gets wrong.

    services/payout_service.py's docstring is about two dangling states: a payouts row stuck
    in 'created' with no txid, and reserved inventory for a send that never happened. A guard
    placed after reserve_inventory() or after the INSERT would create both.

    It is placed after the CLAIM instead, so exactly one worker writes the refusal, and the
    swap lands in 'failed' rather than staying 'payout_pending' -- which would be re-read and
    re-refused on every cycle forever (rule 14: "did nothing" must not look like "did work",
    turned into a loop).

    MUTATION: move the guard below `reserve_inventory(...)` and the hot_reserved assertion
    fails; move it below the INSERT and the payouts-count assertion fails.
    """
    conn = _seed_pending(tmp_path / "boundary.db", to_asset="LTC",
                         payout_address=INVALID_PLACEHOLDERS["truncated"])

    process_pending_payouts(conn, {}, {"LTC": RecordingAdapter()})

    assert conn.execute("SELECT COUNT(*) AS n FROM payouts").fetchone()["n"] == 0
    assert conn.execute("SELECT hot_reserved AS r FROM wallet_inventory WHERE asset='LTC'").fetchone()["r"] == 0.0
    swap = conn.execute("SELECT status, failed_reason FROM swaps WHERE id='s1'").fetchone()
    assert swap["status"] == "failed", "a 'payout_pending' swap would be re-refused every cycle forever"
    assert "refused before send" in swap["failed_reason"]


def test_a_valid_payout_is_still_sent(tmp_path):
    """The other direction, so the guard cannot pass by refusing everything.

    Without this, deleting `if verdict.refuses` from the guard and replacing it with an
    unconditional refusal would leave the whole file green.
    """
    adapter = RecordingAdapter()
    conn = _seed_pending(tmp_path / "good.db", to_asset="LTC", payout_address=LTC_PARTICIPANT)

    process_pending_payouts(conn, {}, {"LTC": adapter})

    assert adapter.sends == [(LTC_PARTICIPANT, 0.0975)]
    assert conn.execute("SELECT status AS s FROM swaps WHERE id='s1'").fetchone()["s"] == "completed"


def test_a_valid_monero_payout_is_not_refused_by_the_send_guard(tmp_path):
    """THE CENTRAL TRAP, AT THE SITE IT WOULD HAVE BEEN WALKED INTO.

    Config.ALLOWED_PAIRS carries ("GRC","XMR"). A blanket `is_valid_address()` at this exact
    line refuses every valid Monero payout -- on a swap whose deposit has already been taken
    and credited, so the customer's money is ours and they cannot be paid.

    MUTATION: replace check_address() in payout_service with address_network.
    is_valid_address() and this goes red while every refusal test above stays green. That
    asymmetry is the whole argument for the table.
    """
    adapter = RecordingAdapter()
    conn = _seed_pending(tmp_path / "xmr.db", to_asset="XMR", payout_address=XMR_PAYOUT)

    process_pending_payouts(conn, {}, {"XMR": adapter})

    assert adapter.sends == [(XMR_PAYOUT, 0.0975)]


def test_a_chain_with_no_validator_is_sent_and_says_it_was_not_checked(tmp_path, caplog):
    """NO_VALIDATOR PASSES THROUGH, LOUDLY -- the decision recorded in the module header.

    Refusing a chain this repository cannot validate would break that chain's payouts
    outright, which is strictly worse than the burn being prevented. The safety net is
    test_every_asset_this_terminal_can_reach_has_a_validator above: the gap fails the suite
    instead of reaching a customer.

    'DOGE' is used precisely because it is not a chain here -- this branch is unreachable for
    every asset that exists, which is exactly why it needs a test to have any behavior at all.

    MUTATION: make check_address() return INVALID for an unknown asset and the sends
    assertion fails.
    """
    adapter = RecordingAdapter()
    conn = _seed_pending(tmp_path / "doge.db", to_asset="DOGE", payout_address=GRC_PAYOUT)

    with caplog.at_level("WARNING"):
        process_pending_payouts(conn, {}, {"DOGE": adapter})

    assert adapter.sends == [(GRC_PAYOUT, 0.0975)]
    assert "NOT CHECKED" in caplog.text


# ---------------------------------------------------------------------------
# THE RECEIVE PATH: where we hand a customer an address to pay into
# ---------------------------------------------------------------------------


class StubSourceChain:
    """A deposit chain whose `getnewaddress` answer is whatever the test seeded."""

    can_spend = True
    payout_refusal = ""

    def __init__(self, derived):
        self.derived = derived
        self.labels = []

    def get_new_address(self, label):
        self.labels.append(label)
        return self.derived

    def validate_address(self, address):
        return bool(address)


class StubDestination:
    can_spend = True
    payout_refusal = ""

    def validate_address(self, address):
        return True


def _quote_db(tmp_path, name):
    conn = connect_db(str(tmp_path / name))
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps, "
        "network_fee_reserve, output_amount_estimate, expires_at, created_at) "
        "VALUES ('q1','GRC','LTC',100.0,0.001,150,0.001,0.0975,'2999-01-01T00:00:00+00:00','2026-09-27T00:00:00+00:00')"
    )
    conn.commit()
    return conn


def test_a_deposit_address_that_cannot_receive_refuses_the_swap_and_writes_nothing(tmp_path):
    """THE RECEIVE-PATH GUARD, and the reason it REFUSES where the send guard does not.

    On the send path a refusal strands a swap whose deposit is already ours. Here NOTHING HAS
    MOVED: no deposit taken, no row written, no instruction shown. A refusal costs a retry;
    letting it through costs a customer's entire deposit, paid into a string that can never
    be spent. The loss is THEIRS and they cannot detect it before paying, which is what makes
    this the worse of the two failures.

    The zero-rows assertion is the same invariant
    tests/test_xrp_swap_attribution.py::test_a_refused_xrp_swap_leaves_no_row_behind pins,
    held here by construction: the guard is a pure check ahead of the INSERT.

    MUTATION: delete the _refuse_unusable_deposit_address() call in deposit_account() and
    both assertions fail.
    """
    conn = _quote_db(tmp_path, "deposit.db")
    source = StubSourceChain(INVALID_PLACEHOLDERS["base58 checksum"])

    with pytest.raises(ValueError, match="NO SWAP WAS CREATED"):
        create_swap(conn, CONFIG_TESTNET_GRC, {"GRC": source, "LTC": StubDestination()}, "q1", LTC_PARTICIPANT)

    assert conn.execute("SELECT COUNT(*) AS n FROM swaps").fetchone()["n"] == 0


def test_a_mainnet_deposit_address_is_refused_when_this_process_is_on_testnet(tmp_path):
    """THE ACCIDENT THAT ACTUALLY HAPPENED, at the site that would have shipped it.

    2026-09-27: `gridcoinresearchd getnewaddress` with no `-testnet` put
    a MAINNET address in the operator's live staking wallet. The address decodes perfectly,
    so a decode-only guard waves it through -- and the customer's real money goes onto a
    chain nothing here is watching.

    CONFIG_TESTNET_GRC carries a Gridcoin TEST port, so expected_network() says testnet
    without this test asserting that string. The refusal message names both networks.

    MUTATION: have deposit_account() call check_address() instead of check_receive_address()
    and this fails while every other receive test stays green.
    """
    conn = _quote_db(tmp_path, "wrongnet.db")
    source = StubSourceChain(GRC_MAINNET_NOBODY_HOLDS)

    with pytest.raises(ValueError, match="NO SWAP WAS CREATED") as raised:
        create_swap(conn, CONFIG_TESTNET_GRC, {"GRC": source, "LTC": StubDestination()}, "q1", LTC_PARTICIPANT)

    assert "mainnet" in str(raised.value) and "testnet" in str(raised.value)
    assert conn.execute("SELECT COUNT(*) AS n FROM swaps").fetchone()["n"] == 0


def test_a_deposit_chain_with_no_validator_still_creates_the_swap(tmp_path, caplog):
    """ADDED AFTER A MUTATION SURVIVED: making the receive guard refuse NO_VALIDATOR killed no
    test, because every receive test used a chain that HAS a validator.

    The decision being pinned is the same one the send path makes and it is in the module
    header: an unvalidatable chain must not be an outage. The safety net is
    test_every_asset_this_terminal_can_reach_has_a_validator, which fails the suite if a
    reachable chain lacks one -- so this branch is unreachable for every asset that exists,
    and needs a test precisely because it would otherwise have no behavior at all.

    'DOGE' is used because it is not a chain here. create_swap() reads the QUOTE's assets, not
    Config.ALLOWED_PAIRS, so a seeded quote is enough.

    MUTATION: refuse on `verdict.unchecked` in _refuse_unusable_deposit_address() and the row
    count drops to 0.
    """
    conn = connect_db(str(tmp_path / "novalidator.db"))
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps, "
        "network_fee_reserve, output_amount_estimate, expires_at, created_at) "
        "VALUES ('q1','DOGE','LTC',100.0,0.001,150,0.001,0.0975,'2999-01-01T00:00:00+00:00',"
        "'2026-09-27T00:00:00+00:00')"
    )
    conn.commit()
    # Address-shaped and unplaceable on a chain with no validator -- exactly what a new
    # chain's getnewaddress looks like to this module on its first day.
    with caplog.at_level("WARNING"):
        swap = create_swap(
            # DOGE_MIN_CONFIRMATIONS is needed because create_swap() reads it AFTER the guard
            # -- which is itself worth recording: the guard passed the unvalidatable chain
            # through, and the next line down failed on an unrelated missing config key. That
            # ordering is what a new chain's first day looks like.
            conn, CONFIG_TESTNET_GRC | {"DOGE_MIN_CONFIRMATIONS": 6},
            {"DOGE": StubSourceChain(GRC_PAYOUT), "LTC": StubDestination()}, "q1", LTC_PARTICIPANT,
        )

    assert swap["status"] == "awaiting_deposit"
    assert conn.execute("SELECT COUNT(*) AS n FROM swaps").fetchone()["n"] == 1
    assert "NOT CHECKED" in caplog.text
    assert NO_VALIDATOR in caplog.text


def test_a_good_deposit_address_still_creates_the_swap(tmp_path):
    """The other direction. Without it, a guard that refused unconditionally would pass."""
    conn = _quote_db(tmp_path, "ok.db")
    source = StubSourceChain(GRC_PAYOUT)

    swap = create_swap(conn, CONFIG_TESTNET_GRC, {"GRC": source, "LTC": StubDestination()}, "q1", LTC_PARTICIPANT)

    assert swap["deposit_address"] == GRC_PAYOUT
    assert swap["status"] == "awaiting_deposit"
    assert source.labels == [f"swap_{swap['id']}"]


def test_an_undecodable_payout_address_is_refused_before_the_daemon_is_asked(tmp_path):
    """create_swap()'s OWN payout guard -- the one the web form reaches.

    open_swap.py's CLI carries a local decode and services/payout_service.py guards the send,
    but routes/swaps.py POSTs straight into create_swap(). This is the earliest point a
    burn can be stopped on the send side, and the only one where nothing has been taken yet.

    `asked == []` is the load-bearing half: the refusal happens with no daemon involved, so
    it works on a host where nothing is running.

    MUTATION: delete the `payout_verdict.refuses` block and this fails on the raise.
    """
    conn = _quote_db(tmp_path, "payoutguard.db")

    class Recording(StubDestination):
        def __init__(self):
            self.asked = []

        def validate_address(self, address):
            self.asked.append(address)
            return True

    destination = Recording()

    with pytest.raises(ValueError, match="cannot receive a LTC payout"):
        create_swap(
            conn, CONFIG_TESTNET_GRC, {"GRC": StubSourceChain(GRC_PAYOUT), "LTC": destination},
            "q1", INVALID_PLACEHOLDERS["bech32 checksum"],
        )

    assert destination.asked == [], "the daemon was asked about an address that decodes as nothing"
    assert conn.execute("SELECT COUNT(*) AS n FROM swaps").fetchone()["n"] == 0


# ---------------------------------------------------------------------------
# the display boundary, which REPORTS and must not hide
# ---------------------------------------------------------------------------


def test_a_stored_deposit_address_that_cannot_receive_is_reported_to_the_customer():
    """The last boundary, over a row that already exists.

    A row can reach this page without ever passing the creation guard -- written by an older
    version of this code, edited by hand, restored from a backup. templates/swap.html:59
    renders the send target only when `problem` is empty, so setting it is what stops a
    customer pasting an unpayable address into their wallet.

    MUTATION: return "" from _address_problem() for a refused verdict and this fails.
    """
    swap = {"from_asset": "GRC", "deposit_address": INVALID_PLACEHOLDERS["base58 checksum"]}

    instruction = deposit_instruction(swap)

    assert instruction["problem"], "the page would have rendered an unpayable address as the send target"
    assert "CANNOT RECEIVE A DEPOSIT" in instruction["problem"]


def test_a_wrong_network_deposit_address_is_still_shown_on_the_page():
    """THE DELIBERATE ASYMMETRY WITH THE CREATION GUARD, and it is not an oversight.

    The creation guard refuses a wrong-network address, because at that moment nothing has
    moved. This page runs much later, over a row that exists -- and a wrong-network address
    STILL DECODES, so a customer may ALREADY HAVE PAID it. Blanking it off the page then
    hides the only string that would let them find their own transaction.

    An UNDECODABLE address cannot have received anything, by construction, which is why the
    test above hides that one and this one does not hide this one.

    MUTATION: make _address_problem() call check_receive_address() and this fails.
    """
    swap = {"from_asset": "GRC", "deposit_address": GRC_MAINNET_NOBODY_HOLDS}

    instruction = deposit_instruction(swap)

    assert instruction["problem"] == ""
    assert instruction["address"] == GRC_MAINNET_NOBODY_HOLDS


def test_the_shared_xrp_account_is_checked_too_and_the_missing_tag_is_reported_first():
    """THE GAP REVIEW FOUND, 2026-09-28. Two branches render an address; one was checked.

    deposit_instruction() has a per-swap `address` branch (BTC/LTC/GRC/XMR) and a shared
    `destination_tag` branch (XRP). _address_problem() was wired into the first on 2026-09-27
    and NOT into the second, so an XRP swap that HAD a tag reported problem="" no matter what
    was in `deposit_address` -- the page printed the account, printed the tag, and said
    nothing.

    THIS BRANCH IS THE WORSE OF THE TWO TO LEAVE UNCHECKED, and that is why it is a gap and
    not a nicety. The XRP account is SHARED BY EVERY SWAP (services/swap_service.py reads it
    from XRP_DEPOSIT_ACCOUNT), so one bad value is every customer on that chain rather than
    one. The per-swap branch's worst case is one swap.

    ORDER IS ASSERTED, NOT JUST PRESENCE. A customer with no tag must not send even to a
    perfect account -- an untagged payment cannot be attributed and the money arrives
    unclaimable -- so when both are wrong the TAG is the sentence that has to reach them.
    Reporting the address complaint there would answer a question they did not ask.

    MUTATION: drop _address_problem() from the tag branch and assertion (2) fails; swap the
    two arms of the conditional and assertion (3) fails. Neither mutation touches the
    `address` branch, which is why the test above cannot stand in for this one.
    """
    tagged_but_unpayable = {
        "from_asset": "XRP",
        "deposit_address": INVALID_PLACEHOLDERS["XRP account zero with a typo"],
        DEPOSIT_TAG_COLUMN: 4242,
    }

    instruction = deposit_instruction(tagged_but_unpayable)

    # (1) the branch really is the tag branch, so this is not measuring the other one
    assert instruction["model"] == "destination_tag"
    # (2) an unpayable SHARED account is reported even though the tag is fine
    assert "CANNOT RECEIVE A DEPOSIT" in instruction["problem"], (
        "the page rendered a shared XRP account that cannot receive a deposit, with no problem set -- "
        "every XRP customer would have been told to pay it"
    )

    # (3) and when BOTH are wrong, the missing tag is what the customer is told
    both_wrong = {**tagged_but_unpayable, DEPOSIT_TAG_COLUMN: None}
    assert "NO DESTINATION TAG" in deposit_instruction(both_wrong)["problem"], (
        "with no tag issued, the instruction a customer needs is 'do not send yet' -- not a complaint "
        "about the account"
    )


def test_a_good_xrp_account_with_a_tag_still_renders_with_no_problem():
    """The control for the test above, so the tag branch cannot pass by flagging everything.

    Without this, hard-coding a problem string into the tag branch would leave the assertions
    above green while breaking every real XRP deposit page -- which is the false-refusal
    failure this whole area is shaped around, arriving through the display instead of the
    guard.
    """
    instruction = deposit_instruction(
        {"from_asset": "XRP", "deposit_address": XRP_HOT_ACCOUNT, DEPOSIT_TAG_COLUMN: 0}
    )

    assert instruction["problem"] == "", instruction["problem"]
    assert instruction["tag"] == 0, "tag 0 is a real tag and must not read as missing"
    assert instruction["address"] == XRP_HOT_ACCOUNT


def test_a_good_deposit_address_renders_with_no_problem():
    """The control, so the display check cannot pass by flagging everything."""
    instruction = deposit_instruction({"from_asset": "GRC", "deposit_address": GRC_PAYOUT})

    assert instruction["problem"] == ""
    assert instruction["address"] == GRC_PAYOUT


# ---------------------------------------------------------------------------
# the platform fee, where the failure direction is the opposite one
# ---------------------------------------------------------------------------


def test_an_unusable_platform_fee_address_charges_no_fee_instead_of_blocking(monkeypatch):
    """NONE, NOT AN EXCEPTION, and the direction is fixed.

    A redeem is time-critical -- the hashlock branch has to be spent before the
    counterparty's timelock expires, and no client in this package implements a refund. So
    refusing a customer's redeem because OUR fee address is malformed would strand their
    whole leg to protect our 1.5%. That is the wrong trade.

    The behavior an UNSET variable already had is extended to an UNUSABLE one, which is the
    only failure direction worth having.

    MUTATION: raise instead of returning None and this fails; return the address anyway and
    the first assertion fails.
    """
    monkeypatch.setenv("PLATFORM_FEE_GRC_ADDRESS", INVALID_PLACEHOLDERS["base58 checksum"])

    fee = usable_platform_fee_address("GRC")

    assert fee.address is None, "an unusable fee address would have been paid, burning the fee"
    # Rule 14: the warning that follows this has to say WHICH failure, because after this
    # change "is unset" would be false two times out of three.
    assert "NOT A USABLE GRC ADDRESS" in fee.why
    assert INVALID_PLACEHOLDERS["base58 checksum"] in fee.why


def test_a_usable_platform_fee_address_is_returned_unchanged(monkeypatch):
    """The other direction: the fee is still collected when the variable is right."""
    monkeypatch.setenv("PLATFORM_FEE_GRC_ADDRESS", GRC_PAYOUT)

    fee = usable_platform_fee_address("GRC")

    assert fee.address == GRC_PAYOUT
    assert "PLATFORM_FEE_GRC_ADDRESS" in fee.why


def test_a_fee_address_on_the_wrong_chain_is_refused_like_a_malformed_one(monkeypatch):
    """A Litecoin bech32 address in PLATFORM_FEE_GRC_ADDRESS is the copy-paste that happens.

    It is a real address, so a "does it decode" check accepts it -- and Gridcoin has no
    bech32 at all, so the output would be unspendable. Same outcome as a broken checksum:
    no fee output, no blocked redeem.
    """
    monkeypatch.setenv("PLATFORM_FEE_GRC_ADDRESS", LTC_PARTICIPANT)

    fee = usable_platform_fee_address("GRC")

    assert fee.address is None
    assert "no bech32" in fee.why


def test_an_unset_fee_variable_still_charges_nothing_and_says_so(monkeypatch):
    """Unchanged behavior, pinned here because this function now has two ways to return None
    and an operator has to be able to tell them apart from the log line alone."""
    monkeypatch.delenv("PLATFORM_FEE_GRC_ADDRESS", raising=False)

    fee = usable_platform_fee_address("GRC")

    assert fee.address is None
    assert "is unset" in fee.why
