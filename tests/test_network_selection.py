"""ONE SWITCH TO MAINNET, and the checks that make one switch safe rather than loaded.

Role: tests (offline; no daemon, no network, no chain)
Reads: modules/network_selection.py, modules/address_network.py
Writes: nothing
Can move funds: no
Mainnet-safe: yes -- nothing here opens a socket. It is ABOUT mainnet and never reaches one.
Live-safe: yes

Operator instruction, 2026-09-28: "we want to be able to switch this entire swap engine to
mainnets with one fale swoop."

The switch is the easy half. The half that loses money is AGREEMENT: a flag says which chain
somebody meant, and only the daemon says which chain the coins are on. Most of this file is
about the disagreements, because the one that matters is silent -- configured testnet, daemon
on mainnet, real coins spent to a testnet-encoded address nobody can pay from, nothing errors.
"""

from __future__ import annotations

import pytest
from modules import address_network, network_selection

TESTNET = network_selection.TESTNET
MAINNET = network_selection.MAINNET


# ---------------------------------------------------------------------------
# Selecting. Unset is TESTNET; a near miss RAISES; only the exact word is mainnet.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_an_unset_or_empty_selection_is_TESTNET(raw):
    """An engine that defaulted to mainnet would put real coins one forgotten export away."""
    assert network_selection.selected_network(raw) == TESTNET


def test_the_exact_words_select_what_they_say():
    assert network_selection.selected_network(TESTNET) == TESTNET
    assert network_selection.selected_network(MAINNET) == MAINNET


@pytest.mark.parametrize("raw", ["main", "MAINNET", "Mainnet", " mainnet", "mainnet ", "prod", "1", "true", "live"])
def test_a_NEAR_MISS_raises_rather_than_quietly_meaning_testnet(raw):
    """THE ASYMMETRY THAT IS THE WHOLE DESIGN, and the case a plain default gets wrong.

    Every string here was somebody TRYING to select mainnet. Reading them as testnet is not the
    safe direction -- it runs the entire engine on a chain the operator does not think it is on,
    which is how a swap gets attempted against balances that are not there. Reading them as
    mainnet is the outcome this module exists to prevent. Refusing is the only answer that
    cannot be wrong, and it costs one reread.

    `MAINNET` uppercase is in this list deliberately. Accepting it would mean accepting
    `Mainnet` and `mAiNnEt`, and the moment the comparison is fuzzy, "did this select mainnet"
    stops having one answer.
    """
    with pytest.raises(network_selection.NetworkSelectionError) as raised:
        network_selection.selected_network(raw)
    assert MAINNET in str(raised.value) and TESTNET in str(raised.value), (
        "the refusal has to name both exact strings, or the operator's next guess is another one"
    )


def test_is_mainnet_is_the_only_place_that_compares_the_string(monkeypatch):
    """rule 8 at its smallest scale: `== "mainnet"` spelled at thirty call sites is one typo
    away from a site that answers False on mainnet forever, and nothing fails when it does."""
    monkeypatch.setenv(network_selection.NETWORK_VARIABLE, MAINNET)
    assert network_selection.is_mainnet()
    monkeypatch.setenv(network_selection.NETWORK_VARIABLE, TESTNET)
    assert not network_selection.is_mainnet()
    monkeypatch.delenv(network_selection.NETWORK_VARIABLE, raising=False)
    assert not network_selection.is_mainnet(), "unset is testnet, here as everywhere"


# ---------------------------------------------------------------------------
# The version bytes are DERIVED from the authority, never re-spelled.
# ---------------------------------------------------------------------------


def test_every_encoded_byte_decodes_back_to_the_network_it_was_asked_for():
    """THE ROUND TRIP IS THE POINT, and it is what a hand-written encode table cannot promise.

    address_network owns the decode direction: given a byte, which network. This module inverts
    it. So every byte it hands out must decode, through the AUTHORITY and not through a copy,
    to the network that was asked for -- otherwise the two have drifted, which is rule 8's
    failure with a delay on it.
    """
    for asset in ("BTC", "LTC", "GRC"):
        for kind in (network_selection.P2PKH, network_selection.P2SH):
            for network in (TESTNET, MAINNET):
                byte = network_selection.base58_version(asset, kind, network)
                assert len(byte) == 1
                table = (address_network.P2PKH_VERSIONS if kind == network_selection.P2PKH
                         else address_network.P2SH_VERSIONS)
                assert table[byte[0]] == network, f"{asset} {kind} {network} -> {byte.hex()}"
                assert asset in address_network.BASE58_VERSION_ASSETS[byte[0]]


def test_testnet_still_answers_the_bytes_this_repository_already_uses():
    """A regression gate on the only two that are already load-bearing everywhere, so wiring
    this module in cannot silently change an address the harness has been producing."""
    assert network_selection.base58_version("GRC", network_selection.P2PKH, TESTNET) == b"\x6f"
    assert network_selection.base58_version("BTC", network_selection.P2PKH, TESTNET) == b"\x6f"
    assert network_selection.base58_version("GRC", network_selection.P2SH, TESTNET) == b"\xc4"


def test_litecoins_TWO_live_p2sh_bytes_are_a_named_choice_and_not_a_dict_ordering():
    """Litecoin declares SCRIPT_ADDRESS and SCRIPT_ADDRESS2 for every network and both are live.
    Decoding must take either; encoding has to pick, and there is nothing in the tables to
    derive the pick from -- they say what a byte MEANS, not what a wallet emits.

    So the pick is named in ENCODE_PREFERENCE with litecoind's own behavior as the reason. What
    this test refuses is the version where it is not named: "whichever the dict yielded first"
    is a decision nobody made and nobody recorded, and it changes when the table is reordered.
    """
    for network, expected in ((MAINNET, 0x32), (TESTNET, 0x3A)):
        assert network_selection.ENCODE_PREFERENCE[("LTC", network_selection.P2SH, network)] == expected
        assert network_selection.base58_version("LTC", network_selection.P2SH, network) == bytes([expected])


def test_every_preference_names_a_byte_the_AUTHORITY_agrees_is_that_network():
    """A preference is allowed to CHOOSE among the authority's bytes and never to invent one.
    Without this, a typo in ENCODE_PREFERENCE would encode addresses for the wrong chain and
    every other test here would still pass, because they all go through the preference."""
    for (asset, kind, network), byte in network_selection.ENCODE_PREFERENCE.items():
        table = (address_network.P2PKH_VERSIONS if kind == network_selection.P2PKH
                 else address_network.P2SH_VERSIONS)
        assert table.get(byte) == network, f"{asset} {kind} {network}: {hex(byte)} is not {network}"
        assert asset in address_network.BASE58_VERSION_ASSETS.get(byte, frozenset())


def test_an_asset_with_no_byte_on_a_network_says_so_rather_than_raising_a_KeyError():
    with pytest.raises(network_selection.NetworkSelectionError) as raised:
        network_selection.base58_version("DOGE", network_selection.P2PKH, MAINNET)
    assert "address_network" in str(raised.value), "and it names where the fix goes"


def test_an_unknown_address_kind_is_refused():
    with pytest.raises(network_selection.NetworkSelectionError):
        network_selection.base58_version("BTC", "p2wpkh", TESTNET)


# ---------------------------------------------------------------------------
# THE AGREEMENT GATE. The half that actually loses money.
# ---------------------------------------------------------------------------


def test_the_daemon_and_the_configuration_agreeing_returns_the_agreed_network():
    assert network_selection.assert_daemon_agrees("GRC", TESTNET, TESTNET) == TESTNET
    assert network_selection.assert_daemon_agrees("GRC", MAINNET, MAINNET) == MAINNET


def test_configured_TESTNET_against_a_MAINNET_daemon_is_refused():
    """THE SILENT ONE, AND THE EXPENSIVE ONE. The engine derives testnet-encoded addresses and
    spends REAL coins to them. Nothing errors, the coins are gone to an address nobody can pay
    from, and the first sign is a balance."""
    with pytest.raises(network_selection.NetworkSelectionError) as raised:
        network_selection.assert_daemon_agrees("GRC", MAINNET, TESTNET)
    message = str(raised.value)
    assert "REAL coins" in message, "the refusal has to say what it costs, not just that it differs"
    assert "Nothing was derived, signed or sent" in message


def test_configured_MAINNET_against_a_TESTNET_daemon_is_refused_too():
    """The other direction is recoverable and still not allowed: a mismatch is a mismatch, and
    an engine that tolerated one direction would be an engine whose gate has a hole in it."""
    with pytest.raises(network_selection.NetworkSelectionError):
        network_selection.assert_daemon_agrees("GRC", TESTNET, MAINNET)


@pytest.mark.parametrize("silence", [None, "", "   "])
@pytest.mark.parametrize("configured", [TESTNET, MAINNET])
def test_SILENCE_IS_NOT_AGREEMENT_for_either_configured_network(silence, configured):
    """Including when mainnet is what was configured, which is the tempting exception.

    adaptor_regtest_verify.py's posture -- "an absence of evidence is treated as mainnet" --
    was obviously right there, because silence could only mean "maybe mainnet". Here the
    configured network could BE mainnet, and it reads as consent. It is not: a daemon that will
    not say which chain it is on is a daemon this engine has not identified, and identifying it
    is one RPC call.
    """
    with pytest.raises(network_selection.NetworkSelectionError) as raised:
        network_selection.assert_daemon_agrees("GRC", silence, configured)
    assert "Silence is not agreement" in str(raised.value)


# ---------------------------------------------------------------------------
# "Can we go mainnet?" is a LIST, and it is measured.
# ---------------------------------------------------------------------------


def test_the_blocker_list_names_the_module_that_actually_blocks_it():
    """modules/atomic_htlc_scripts.py RE-ENCODES every address it is handed to the testnet
    version byte, structurally, on the order path, and says so in its own header. Selecting
    mainnet while that is true does not fail -- it turns a mainnet address into a testnet one
    and pays it. If it ever drops off this list, that is either the fix or a header that lies,
    and both are worth stopping for."""
    blockers = network_selection.mainnet_blockers()
    assert "swap_terminal/modules/atomic_htlc_scripts.py" in blockers
    assert blockers == sorted(blockers), "sorted, so two runs are comparable line by line"


def test_a_module_that_merely_QUOTES_another_headers_declaration_is_not_counted(tmp_path):
    """THE FALSE POSITIVE THIS SCAN SHIPPED WITH, found by running it: the first version matched
    the marker anywhere in the header text, so network_selection.py flagged ITSELF -- it is
    mainnet-safe, and its docstring quotes atomic_htlc_scripts.py's declaration mid-sentence.

    Exactly the defect fixed in the harness's own output hours earlier, where a paragraph
    quoting an obsolete denial was still a screen carrying that denial. Quotation marks survive
    a scan no better than they survive a skim.
    """
    (tmp_path / "talks_about_it.py").write_text(
        '"""A module that is fine.\n\nMainnet-safe: yes\n\n'
        'It discusses another file whose header reads ("Mainnet-safe: NO, and structurally so").\n"""\n'
    )
    (tmp_path / "actually_unsafe.py").write_text('"""Role: thing\n\nMainnet-safe: NO, it re-encodes.\n"""\n')
    (tmp_path / "indented_header.py").write_text('"""Role: thing\n       Mainnet-safe: no\n"""\n')

    found = network_selection.mainnet_blockers(tmp_path)
    assert found == ["actually_unsafe.py", "indented_header.py"], (
        "a declaration starts a line, optionally indented; anything mid-sentence is prose ABOUT one"
    )


def test_this_module_does_not_flag_itself():
    """The regression that the test above generalizes, pinned on the real tree as well -- a
    tmp_path fixture cannot catch the scan being pointed at a different root."""
    assert "swap_terminal/modules/network_selection.py" not in network_selection.mainnet_blockers()
