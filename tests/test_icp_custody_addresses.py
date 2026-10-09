"""icp_custody_addresses: the read-only comparison, and that it stays read-only.

Role: tests (read-only)
Reads: icp_custody_addresses
Writes: nothing
Can move funds: no
Mainnet-safe: yes -- no dfx, no replica, no daemon, no network.

WHAT IS WORTH GUARDING in a file that only prints. Three things, and none of them
is the formatting:

  the parse refuses rather than returning an empty key. An empty key would derive
  three addresses that look exactly as real as correct ones, which is the quiet
  failure this whole project keeps meeting.

  one chain's dead daemon does not stop the comparison for the others, and the
  reason lands in that chain's cell rather than being swallowed into a blank gap
  (rule 14: `(none)` is a result, an empty space is ambiguous).

  the file calls NOTHING that creates an address. That is asserted by checking
  which adapter method it uses, because "read-only" is the claim the operator is
  trusting when they run it against a live wallet.
"""

from __future__ import annotations

import inspect

import pytest
from modules.address_authority import expected_network
from modules.address_network import MAINNET, TESTNET
from modules.pubkey_address import address_from_public_key
from network_target import UNCONFIGURED_PORT
from regtest.keys import generate_key
from valid_addresses import base58_testnet, bech32_address

import icp_custody_addresses as subject
from swap_terminal.chains.icp import ICPCallFailed

#: dfx's real shape for this call, copied from the operator's own run on
#: 2026-10-06 -- a variant wrapping a record, not a bare value.
REAL_OUTPUT = """(
  variant {
    Ok = record {
      derivation_path = vec {};
      key_name = "dfx_test_key";
      public_key_hex = "031c72cccf80f29f74f8fa19e3e4a00e317575683fcb114558241707392703c1f0";
    }
  },
)"""

REAL_KEY_HEX = "031c72cccf80f29f74f8fa19e3e4a00e317575683fcb114558241707392703c1f0"


def test_the_real_dfx_output_yields_the_key_the_operator_saw():
    """Pinned against text dfx actually emitted, not against text shaped like it."""
    got = subject.canister_public_key("be2us-64aaa-aaaaa-qaabq-cai", "icp-replica", 60.0,
                                      call=lambda *a: REAL_OUTPUT)
    assert got == REAL_KEY_HEX
    assert len(bytes.fromhex(got)) == 33
    assert bytes.fromhex(got)[0] in (0x02, 0x03)


def test_the_argument_sent_is_the_root_derivation_path():
    """An empty vec. A different path is a different key and therefore different addresses."""
    seen = []
    subject.canister_public_key("c", "s", 1.0, call=lambda canister, method, arg: (seen.append((canister, method, arg)), REAL_OUTPUT)[1])
    assert seen == [("c", "public_key", "(vec {})")]


@pytest.mark.parametrize("junk", ["", "(variant { Err = \"no key\" })", "public_key_hex = ''", "nonsense"])
def test_output_with_no_key_in_it_raises_rather_than_returning_empty(junk):
    """THE ONE THAT MATTERS: an empty key derives three plausible-looking addresses."""
    with pytest.raises(ICPCallFailed, match="not an empty key"):
        subject.canister_public_key("c", "s", 1.0, call=lambda *a: junk)


def test_one_dead_daemon_does_not_hide_the_other_chains():
    """The reason goes in the cell. A blank would be ambiguous between none and broken."""

    class Dead:
        def own_address(self):
            raise ConnectionRefusedError("daemon not listening")

    class Alive:
        def own_address(self):
            return "an-address-this-wallet-already-has"

    class Empty:
        def own_address(self):
            return ""

    cells = subject.desk_addresses({"BTC": Alive(), "LTC": Dead(), "GRC": Empty()})
    assert cells["BTC"] == "an-address-this-wallet-already-has"
    assert "unreadable" in cells["LTC"] and "ConnectionRefusedError" in cells["LTC"]
    assert cells["GRC"].startswith("(none")
    assert all(cells[a] for a in ("BTC", "LTC", "GRC")), "no cell may be blank"


def test_a_missing_adapter_names_the_variables_that_would_configure_it():
    """Not "no adapter" alone -- an operator reading that has nothing to go check."""
    cells = subject.desk_addresses({})
    for asset in ("BTC", "LTC", "GRC"):
        assert "no adapter" in cells[asset]
        assert "RPC" in cells[asset], f"{asset} cell names no variable: {cells[asset]!r}"


def test_the_only_adapter_method_this_file_calls_is_the_read_only_one():
    """THE READ-ONLY CLAIM, asserted rather than documented.

    chains/base.RPCAdapter.own_address() uses listreceivedbyaddress and
    getaddressesbylabel and its docstring says why it must never be getnewaddress
    ("would answer in one call and DERIVES a key"). This file's promise to an
    operator running it against a live wallet is that it calls nothing else, so a
    recorder rejects every other attribute access.
    """
    calls = []

    class OnlyOwnAddress:
        def __getattr__(self, name):
            calls.append(name)
            if name != "own_address":
                raise AssertionError(f"icp_custody_addresses called adapter.{name}, which is not read-only")
            return lambda: "ok"

    subject.desk_addresses({"BTC": OnlyOwnAddress(), "LTC": OnlyOwnAddress(), "GRC": OnlyOwnAddress()})
    assert calls == ["own_address"] * 3


# ---------------------------------------------------------------------------
# comparability(): the defect that shipped, and the four verdicts
# ---------------------------------------------------------------------------

# The operator's live run on 2026-10-06 is what proved the first version's
# explanation wrong, and the four addresses below stand for the four KINDS that run
# contained -- derived here rather than pasted, for two reasons.
#
# The honest one: the test is about address KIND, not about those particular
# strings. What made the old explanation wrong is that a bech32 SegWit address and
# a legacy P2PKH address can never be equal, which is true of any pair of them.
#
# The mechanical one: tests/test_address_literals_are_valid's ceiling is at 60 of
# 60 and pasting them took it to 63. Raising a ceiling to make a check pass is what
# rule 19 forbids outright, so the fixtures moved to the derivation helpers that
# gate's own failure message points at ("Use tests/valid_addresses.py rather than
# writing one -- a derived address cannot be mistyped and says what it is for").
#
# The live strings are kept, truncated, in comparability()'s own docstring, which is
# where a reader looking for the provenance will be.
#
#   derived (the canister column)  mp7vKdjG…8vNJKE   legacy P2PKH testnet
#   desk BTC                       bcrt1q…cxg7lh     bech32, REGTEST hrp
#   desk LTC                       rltc1q…6g846ja    bech32, Litecoin REGTEST hrp
#   desk GRC                       mrJBUvRG…r6UipK   legacy P2PKH testnet
LIVE_DERIVED = base58_testnet("the canister threshold key, derived for the desk")
LIVE_DESK_BTC = bech32_address("bcrt", "the desk's own btc regtest wallet address")
LIVE_DESK_LTC = bech32_address("rltc", "the desk's own ltc regtest wallet address")
LIVE_DESK_GRC = base58_testnet("the desk's own grc testnet wallet address")


@pytest.mark.parametrize("desk,hrp", [(LIVE_DESK_BTC, "bcrt"), (LIVE_DESK_LTC, "rltc")])
def test_a_bech32_desk_address_is_not_comparable_rather_than_a_mismatch(desk, hrp):
    """THE DEFECT THAT SHIPPED, pinned so it cannot come back.

    The first version printed a bare `no` for these two rows plus one blanket
    sentence saying the addresses differ because the desk's were generated by its
    own wallets. That sentence was wrong for exactly these rows: the desk holds
    bech32 SegWit and modules/pubkey_address derives legacy P2PKH, so they could
    never be equal and `no` did not mean what a reader would take it to mean --
    before an armed-state decision.
    """
    verdict, why = subject.comparability(LIVE_DERIVED, desk)
    assert verdict == "n/a"
    assert "not comparable" in why
    assert hrp in why, "the verdict must name the HRP it found, not just the kind"
    assert "TYPE" in why, "it must say that moving the desk here changes the address type too"


def test_two_p2pkh_addresses_on_one_network_are_an_actual_comparison():
    """The GRC row -- the only one of the three that compared anything."""
    verdict, why = subject.comparability(LIVE_DERIVED, LIVE_DESK_GRC)
    assert verdict == "differs"
    assert "both P2PKH" in why and "testnet" in why


def test_an_identical_address_reads_SAME_and_says_what_that_means():
    """A match is the one answer that would change what the operator does."""
    verdict, why = subject.comparability(LIVE_DERIVED, LIVE_DERIVED)
    assert verdict == "SAME"
    assert "already on" in why


@pytest.mark.parametrize("cell", ["", "(none -- the wallet has no address to read)", "(unreadable: X: y)"])
def test_a_cell_that_is_not_an_address_is_not_scored_as_a_mismatch(cell):
    """`differs` would claim a comparison happened against a daemon that was down."""
    verdict, why = subject.comparability(LIVE_DERIVED, cell)
    assert verdict == "n/a"
    assert "no desk address" in why


def test_the_hrp_table_is_not_reimplemented_here():
    """Rule 8: modules/address_network owns the HRPs, including rltc.

    `rltc` was MISSING from that table until 2026-09-27 and three call sites picked
    it up at once when it was added. A second copy in this file would reintroduce
    exactly that gap, so this asserts the classifier is the shared one by checking
    that a Litecoin REGTEST address -- the case that was missing -- is recognized.
    """
    source = inspect.getsource(subject.comparability)
    assert "decode_segwit_address" in source

    # THE DOCSTRING IS STRIPPED BEFORE THE HRP CHECK, and that is not a loophole --
    # it is the distinction the check is about. comparability()'s docstring QUOTES the
    # live run that motivated it, bech32 addresses and all, which is exactly the
    # provenance rule 1 asks for. What must not appear is an HRP in the CODE, because
    # that would mean the function is deciding what bech32 looks like instead of
    # asking modules/address_network. The first version of this assertion scanned the
    # whole source and failed on its own subject's docstring.
    body = source.replace(subject.comparability.__doc__ or "", "")
    for hrp in ("bcrt", "rltc", "tltc", "tb1"):
        assert hrp not in body, (
            f"comparability() names the HRP {hrp!r} in its code, which means it is deciding what "
            f"bech32 looks like instead of asking modules/address_network -- the module that owns "
            f"the table, and that was missing 'rltc' until 2026-09-27"
        )


# ---------------------------------------------------------------------------
# THE NETWORK EACH CHAIN'S ADDRESS IS DERIVED AGAINST, which was one value for
# all three until 2026-10-09 because it read a setting that does not exist.


def test_each_chain_derives_against_the_network_its_own_port_names(monkeypatch):
    """THE DEFECT: `Config.NETWORK` has never existed, so `hasattr` was always False.

        network = Config.NETWORK if hasattr(Config, "NETWORK") else "testnet"

    config.Config has no NETWORK attribute and nothing in the tree sets one, so
    that expression returned the literal "testnet" every time it ran, for every
    chain. It READS as "respects the configured network, defaulting to testnet" --
    a sentence about a setting that is not there.

    IT MATTERS BECAUSE `network` PICKS THE VERSION BYTE.
    modules/pubkey_address.P2PKH_VERSION_FOR is keyed (asset, network): GRC
    mainnet is 0x3E and GRC testnet is 0x6F. Measured on a host with GRC on 15715
    and LTC on 9332 -- both MAINNET ports -- and BTC on 18443:

        BTC  movVRv8XgK7DYx8y4Md2qx534HRXzjZbo8
        LTC  movVRv8XgK7DYx8y4Md2qx534HRXzjZbo8   <- the same address
        GRC  movVRv8XgK7DYx8y4Md2qx534HRXzjZbo8   <- the same address

    Three identical testnet addresses for three different chains. comparability()
    then reported "different networks" and every row came back n/a, with nothing
    on screen saying why -- the "0 of 3 rows were an actual comparison" outcome.

    THE MUTATION THIS CATCHES is a return to one network for all three: the old
    expression gives TESTNET for GRC here, and the first assertion fails.

    SEEDED THROUGH Config.RPC AND DRIVEN THROUGH networks_for(), WHICH IS THE
    WHOLE POINT AND WHICH I GOT WRONG FIRST. The original version of this test
    called expected_network() directly and PASSED against the broken code -- which
    never called that function at all. Reinstating `Config.NETWORK` produced zero
    failures, so the mutation caught the test rather than the code. The decision
    had to come out of main() before anything could assert on it (rule 10).
    """
    monkeypatch.setitem(subject.Config.RPC["GRC"], "port", 15715)   # GRC mainnet
    monkeypatch.setitem(subject.Config.RPC["BTC"], "port", 18443)   # BTC regtest
    monkeypatch.setitem(subject.Config.RPC["LTC"], "port", 9332)    # LTC mainnet

    networks = subject.networks_for(dict(subject.Config.RPC))

    assert networks == {"BTC": TESTNET, "LTC": MAINNET, "GRC": MAINNET}, (
        f"each chain derives against the network its OWN port names, and these three do not "
        f"agree; got {networks}"
    )
    assert len(set(networks.values())) > 1, (
        "one value for all three IS the defect -- that is what Config.NETWORK's always-False "
        "hasattr produced, and a test that cannot tell three-the-same from three-correct is "
        "the one I wrote first"
    )


def test_the_three_version_bytes_are_not_the_same_byte(monkeypatch):
    """The consequence, asserted on the ADDRESSES rather than on the network names.

    A network name is an intermediate value; what reached the operator's screen was
    three identical strings. This derives all three from ONE public key -- so any
    difference between the outputs is the version byte and nothing else -- and
    requires the mainnet-configured chain to land somewhere different from the
    regtest-configured one.

    THE SHARED KEY IS THE CONTROL. Three different keys would produce three
    different addresses whatever the version byte did, and this test would pass
    against the broken code.
    """
    monkeypatch.setitem(subject.Config.RPC["GRC"], "port", 15715)   # mainnet
    monkeypatch.setitem(subject.Config.RPC["BTC"], "port", 18443)   # regtest

    networks = subject.networks_for(dict(subject.Config.RPC))
    public_key = generate_key().public_key
    grc = address_from_public_key(public_key, "GRC", networks["GRC"])
    btc = address_from_public_key(public_key, "BTC", networks["BTC"])

    assert grc != btc, (
        f"one public key derived the same address on a MAINNET-configured GRC daemon and a "
        f"REGTEST-configured BTC daemon ({grc}) -- which is the defect exactly: one network "
        f"for every chain"
    )
    assert grc.startswith("S"), f"GRC mainnet P2PKH starts with S, got {grc}"
    assert btc[0] in "mn", f"BTC testnet P2PKH starts with m or n, got {btc}"


def test_an_unconfigured_port_falls_back_to_testnet_rather_than_guessing_mainnet(monkeypatch):
    """The direction of the fallback, which is a safety choice and not an accident.

    expected_network() answers None for an UNCONFIGURED port and for an
    UNRECOGNIZED one -- its own docstring refuses to call a mainnet daemon on a
    custom -rpcport "not mainnet". The caller turns None into TESTNET.

    That direction is deliberate and this pins it: guessing MAINNET would print an
    address in the format an operator might FUND, and these are derivations of a
    local dfx_test_key that controls nothing on any real chain. Guessing testnet
    prints one that is visibly throwaway. It is the cheaper way to be wrong.
    """
    monkeypatch.setitem(subject.Config.RPC["GRC"], "port", UNCONFIGURED_PORT)
    table = dict(subject.Config.RPC)

    assert expected_network("GRC", table) is None, "an unconfigured port establishes nothing"
    assert subject.networks_for(table)["GRC"] == TESTNET, (
        "and the fallback must be TESTNET -- a mainnet guess prints a FUNDABLE address for a key "
        "that controls nothing"
    )
