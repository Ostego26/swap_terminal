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

import pytest

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
