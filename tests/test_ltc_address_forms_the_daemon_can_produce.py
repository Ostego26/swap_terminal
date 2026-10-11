"""Every LTC address form litecoind can produce is accepted, on both swap legs.

Role: test (pure; no socket, no subprocess, no chain, no database)
Reads: modules/address_authority.py, modules/address_network.py
Writes: nothing
Can move funds: no. Every function here decodes a string and returns a verdict.
Mainnet-safe: yes -- every address is derived on a TEST network by construction.

=============================================================================
A FALSE REFUSAL ON THIS CHAIN IS WORSE THAN THE BURN THE CHECK PREVENTS
=============================================================================

modules/address_network.py records what this cost on 2026-09-27. The operator
ran a BTC->LTC swap against litecoind 0.21.4 and the HTLC contract address
litecoind ITSELF produced for the funded leg was

    QYqEyFb1v76QraQ3uo5wVkGtJrC4Rf5vU3        21 bytes, version 0x3a

and P2SH_VERSIONS read it as belonging to NO NETWORK. The same day, `rltc` --
Litecoin's regtest hrp, its own exactly as Bitcoin's is `bcrt` -- was missing
from the bech32 table, so every Litecoin regtest payout address read as "not
decodable as base58check". That file's own words: "a burn costs one payout, a
false refusal breaks a working chain for every customer on it."

LITECOIN IS THE ONLY ONE OF THE THREE WITH TWO LIVE P2SH VERSION BYTES, and it
ENCODES new P2SH addresses with the second -- SCRIPT_ADDRESS2, 0x32 on mainnet
and 0x3A on testnet and regtest, read off litecoin-project/litecoin's own
chainparams.cpp. So the form that was missing is the form the daemon hands an
operator by default, which is why it was a real refusal and not a hypothetical
one, and why this file exists as a standing gate rather than as a note.

=============================================================================
BOTH LEGS, BECAUSE THEY ASK DIFFERENT QUESTIONS
=============================================================================

check_address() is the PAYOUT guard: the address came from outside and the
network is whatever the customer's wallet is on, so it decodes and does not
compare networks. check_receive_address() is the DEPOSIT guard: the address came
from our own daemon, so a network mismatch is the accident worth refusing --
`gridcoinresearchd getnewaddress` with no `-testnet` put a MAINNET address in
the operator's live staking wallet on 2026-09-27.

A form that passes one and not the other is still a broken chain, in one
direction, so every form below is run through both.

=============================================================================
DERIVED, NEVER TYPED
=============================================================================

Every address comes from tests/valid_addresses.py. A literal can be mistyped in
its checksum and nothing notices until something tries to pay it; a derivation
cannot, and the phrase it is derived from says what the address is for.
tests/test_address_literals_are_valid.py counts address-shaped literals in this
tree and holds the count down, so spelling one here would fail that file as well
as being the defect rule 19 names.
"""

from __future__ import annotations

import pytest
from modules.address_authority import check_address, check_receive_address
from modules.address_network import (
    BASE58_VERSION_ASSETS,
    BECH32_HRPS_BY_ASSET,
    MAINNET,
    P2SH_VERSIONS,
    TESTNET,
)
from valid_addresses import (
    LTC_P2SH_SCRIPT_ADDRESS2,
    LTC_P2SH_TESTNET,
    LTC_PARTICIPANT,
    LTC_REGTEST_DEPOSIT,
    LTC_REGTEST_TAPROOT,
    LTC_TAPROOT_PAYOUT,
    base58_testnet,
)

#: EVERY FORM A LITECOIN DAEMON CAN PRODUCE OR DECODE ON A TEST NETWORK, as
#: (what it is, the address, the daemon setting or script type that yields it).
#: The third element is there so a failure names which `getnewaddress` or which
#: contract would have produced the refused string -- rule 14, applied to an
#: assertion message.
#:
#: Taproot IS included. Litecoin activated taproot, and even on a build whose
#: `getnewaddress` cannot emit a `tltc1p...` a CUSTOMER's payout address can be
#: one, which is the half that matters: this table is what the payout guard is
#: asked about. modules/address_network.py records the outage bech32m's absence
#: was causing -- a swap paying out to a taproot address could not be CREATED at
#: all, ValueError, no row written.
LTC_ADDRESS_FORMS = (
    ("legacy P2PKH, version 0x6f", base58_testnet("ltc testnet legacy payout"),
     "`getnewaddress \"\" legacy`"),
    ("P2SH SCRIPT_ADDRESS, version 0xc4", LTC_P2SH_TESTNET,
     "a P2SH script address on the older of Litecoin's two version bytes"),
    ("P2SH SCRIPT_ADDRESS2, version 0x3a", LTC_P2SH_SCRIPT_ADDRESS2,
     "`getnewaddress \"\" p2sh-segwit`, and the HTLC contract address "
     "`decodescript` returns -- the form that read as NO NETWORK until 2026-09-27"),
    ("bech32 v0, hrp tltc", LTC_PARTICIPANT,
     "`getnewaddress \"\" bech32` on testnet"),
    ("bech32 v0, hrp rltc", LTC_REGTEST_DEPOSIT,
     "`getnewaddress \"\" bech32` on REGTEST -- the hrp that was missing on 2026-09-27"),
    ("bech32m taproot, hrp tltc", LTC_TAPROOT_PAYOUT,
     "a P2TR payout address a customer can supply on testnet"),
    ("bech32m taproot, hrp rltc", LTC_REGTEST_TAPROOT,
     "a P2TR address on regtest"),
)


@pytest.mark.parametrize(("form", "address", "produced_by"), LTC_ADDRESS_FORMS)
def test_every_LTC_address_form_is_VALID_as_a_payout_destination(form, address, produced_by):
    """The payout leg. A refusal here is a customer who cannot be paid.

    MUTATION: remove 0x3A from modules/address_network.P2SH_VERSIONS and
    BASE58_VERSION_ASSETS and the SCRIPT_ADDRESS2 parameter fails with
    "UNDETERMINED" -- which is the 2026-09-27 state, reproduced. Run and
    confirmed 2026-10-11.
    """
    verdict = check_address("LTC", address)

    assert verdict.state == "VALID", (
        f"an LTC {form} address -- {produced_by} -- came back {verdict.state}: {verdict.why}. "
        f"A payout address this repository cannot place is a customer who cannot be paid, and "
        f"a false refusal breaks the chain for everyone on it rather than costing one payout"
    )
    assert not verdict.refuses, f"{form} is refused: {verdict.why}"
    assert not verdict.unchecked, (
        f"{form} passed WITHOUT being verified ({verdict.state}): {verdict.why}. That is the "
        f"UNDETERMINED state Litecoin's `rltc` and 0x3A both produced on the operator's live "
        f"regtest swap -- it proceeds, which is right, but it means nothing checked the address"
    )
    assert verdict.network == TESTNET, (
        f"{form} decoded as network {verdict.network!r}. Every fixture in this file is on a "
        f"TEST network by construction; a mainnet answer means the version byte or hrp table "
        f"has moved"
    )


@pytest.mark.parametrize(("form", "address", "produced_by"), LTC_ADDRESS_FORMS)
def test_every_LTC_address_form_is_VALID_as_a_deposit_address_on_testnet(form, address, produced_by):
    """The deposit leg, where the NETWORK half of the check runs as well.

    The expected network is TESTNET because the operator's litecoind answers on
    19332, which network_target.CHAIN_PORTS classifies as a test port -- and
    because modules/address_network.py folds Litecoin's regtest hrp into TESTNET
    on purpose: this question is "is this an address it is safe to lose coins
    on", and `rltc` and `tltc` have the same answer to that one. (The question
    they have OPPOSITE answers to is "which addresses can this running daemon
    pay", and chains/daemon_network.PAYABLE_BECH32_PREFIX is what answers it.)
    """
    verdict = check_receive_address("LTC", address, TESTNET)

    assert verdict.state == "VALID", (
        f"an LTC {form} address -- {produced_by} -- cannot be handed to a customer as a "
        f"deposit target: {verdict.why}. services/swap_service._refuse_unusable_deposit_address() "
        f"RAISES on this, so the swap is not created at all"
    )
    assert not verdict.refuses, f"{form} is refused on the receive path: {verdict.why}"


@pytest.mark.parametrize(("form", "address", "produced_by"), LTC_ADDRESS_FORMS)
def test_a_testnet_LTC_address_is_REFUSED_as_a_mainnet_deposit_address(form, address, produced_by):
    """The other direction, so the test above cannot be passed by accepting everything.

    Without this, a mutation that made check_receive_address() ignore the
    expected network would satisfy both tests above -- and the accident it
    exists to catch is a daemon on the WRONG NETWORK handing a customer an
    address for a chain nothing here is watching, which is the one failure
    whose loss lands on them and which they cannot detect before paying.
    """
    verdict = check_receive_address("LTC", address, MAINNET)

    assert verdict.refuses, (
        f"a TESTNET {form} address ({produced_by}) was accepted as a deposit address on a "
        f"terminal that believes it is on MAINNET: {verdict.state} -- {verdict.why}"
    )


def test_both_of_Litecoins_P2SH_version_bytes_are_in_the_tables_and_belong_to_LTC_alone():
    """The entries themselves, named, because the table is the thing that was short.

    Asserted on BOTH tables rather than on one: P2SH_VERSIONS decides the
    network and BASE58_VERSION_ASSETS decides which chains a byte can belong
    to, and 0x32/0x3A are the only base58 bytes in this repository that name
    one chain on a shared test network. Bitcoin and Gridcoin declare no
    SCRIPT_ADDRESS2 at all, which is what makes that narrowing legitimate.
    """
    assert P2SH_VERSIONS[0x3A] == TESTNET, (
        "0x3A is Litecoin's testnet AND regtest SCRIPT_ADDRESS2. It was absent until "
        "2026-09-27 and the contract address litecoind produced for a funded HTLC leg read as "
        "belonging to no network"
    )
    assert P2SH_VERSIONS[0x32] == MAINNET, "0x32 is Litecoin's mainnet SCRIPT_ADDRESS2"
    assert BASE58_VERSION_ASSETS[0x3A] == frozenset({"LTC"})
    assert BASE58_VERSION_ASSETS[0x32] == frozenset({"LTC"})
    # The older byte is SHARED with Bitcoin, and that difference is the reason
    # BASE58_VERSION_ASSETS holds a set per byte rather than one asset or None.
    assert BASE58_VERSION_ASSETS[0x05] == frozenset({"BTC", "LTC"})
    assert "LTC" in BASE58_VERSION_ASSETS[0xC4]


def test_all_three_of_Litecoins_bech32_hrps_are_known():
    """ltc, tltc and rltc. `rltc` is the one whose absence refused every regtest payout."""
    assert BECH32_HRPS_BY_ASSET["LTC"] == {"ltc": MAINNET, "tltc": TESTNET, "rltc": TESTNET}, (
        f"Litecoin's hrp table is {BECH32_HRPS_BY_ASSET['LTC']}. Regtest is its own hrp, "
        f"exactly as Bitcoin's is `bcrt` and not `tb`; without `rltc` an unknown hrp falls "
        f"through the bech32 branch entirely and is then tried as base58"
    )


def test_a_LITECOIN_TESTNET_address_is_not_accepted_as_a_BITCOIN_payout():
    """The burn the asset-keyed hrp table exists to prevent, measured on LTC's own form.

    `tltc1...` handed to BTC is a perfectly valid address with a perfectly good
    checksum. It is just not Bitcoin's, so the only thing that can catch it is
    the hrp belonging to an ASSET -- which is why BECH32_HRPS_BY_ASSET is keyed
    by asset rather than being one flat hrp -> network map, and why a flat map
    could not see this at all.

    THE FIXTURE IS THE TESTNET ONE AND NOT A MAINNET `ltc1...`, and the first
    draft of this test got that wrong in two ways at once: it spelled a mainnet
    literal, which tests/valid_addresses.py's own header forbids ("a mainnet
    address in a fixture is one copy-paste away from a real payout"), and the
    literal's checksum did not even decode -- so
    tests/test_address_literals_are_valid.py failed it twice over, on the count
    AND on the decode. That is rule 19's exact case working as intended: "if
    your change needs a new entry to pass, your change is the defect." The
    cross-chain question does not need a mainnet address to be asked.
    """
    verdict = check_address("BTC", LTC_PARTICIPANT)

    assert verdict.refuses, (
        f"a Litecoin bech32 address was accepted as a Bitcoin payout destination: "
        f"{verdict.state} -- {verdict.why}. Those coins do not come back"
    )
    assert "LTC" in verdict.why or "Litecoin" in verdict.why or "tltc" in verdict.why, (
        f"the refusal must say WHOSE address it is, not just that it is wrong -- an operator "
        f"told 'invalid' goes looking for a typo that is not there: {verdict.why}"
    )
