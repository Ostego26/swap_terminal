"""transactions.py says which chain it is about to bill you for.

Role: test (pure string decision; no socket, no daemon, no database)
Reads: transactions.py
Writes: nothing
Can move funds: no
Mainnet-safe: yes

WHY THIS IS A TEST AND NOT A COMMENT

transactions.py produces cost-basis data from MAINNET Gridcoin transactions.
Measured 2026-09-25 on the operator's host: load_dotenv() there reads
swap_terminal/swap_terminal/.env, which does not exist, while the .env that
carries GRIDCOIN_RPC_PORT=25715 lives in grc-sol-swap/abstergo_exchange/ and is
never seen. RPC_PORT therefore fell through to its default of 25779 -- the
rpcport of grctest/testnet3, one of FOUR different testnet ports configured on
that machine (25715 twice, 9876, 25779).

Nothing failed loudly, because a wrong port gives connection-refused, which
reads as "no transactions found". For a tax report, an empty result is not
harmless: it is a document that omits everything.

The number is therefore never printed alone, and these tests pin that.
"""

import importlib
import sys
import types

import pytest

for _name in ("tkinter", "tkinter.messagebox", "tkinter.ttk"):
    sys.modules.setdefault(_name, types.ModuleType(_name))


def target_for(monkeypatch, port: str) -> str:
    """Re-import transactions.py with GRIDCOIN_RPC_PORT set, and read its banner.

    Re-imported rather than monkeypatched because RPC_PORT is read at module
    scope -- which is the import-time-configuration hazard that produced this
    bug in the first place, so the test exercises it the way it really behaves.
    """
    monkeypatch.setenv("GRIDCOIN_RPC_PORT", port)
    sys.modules.pop("transactions", None)
    return importlib.import_module("transactions").describe_rpc_target()


def test_mainnet_is_named_as_the_one_cost_basis_needs(monkeypatch):
    line = target_for(monkeypatch, "15715")
    assert "MAINNET" in line
    assert "TESTNET" not in line


@pytest.mark.parametrize("port", ["25715", "25779", "9876"])
def test_every_testnet_port_on_that_machine_is_called_out(monkeypatch, port):
    """All four configured ports, not just the one the code happened to default to.

    25715 is what the operator's own .env carries, 25779 is what this file
    defaulted to, and 9876 is a third conf. A check that only knew one of them
    would stay silent for the other two.
    """
    line = target_for(monkeypatch, port)
    assert "TESTNET" in line
    assert "15715" in line, "the banner must name the mainnet port to switch to"
    assert "NOT your real transaction history" in line


def test_the_testnet_warning_names_the_only_env_file_this_module_reads(monkeypatch):
    """The bug was a .env in the wrong directory, so the message gives the path.

    Telling an operator to "set it in .env" when three .env files exist and
    only one is read is how the original mistake repeats.
    """
    line = target_for(monkeypatch, "25715")
    assert "ONLY .env this file reads" in line
    assert "swap_terminal/.env" in line


def test_an_unrecognized_port_is_not_silently_blessed(monkeypatch):
    """Not mainnet, not a known test port -- say so rather than imply either."""
    line = target_for(monkeypatch, "40000")
    assert "MAINNET" not in line
    assert "not a port this file recognizes" in line
    assert "15715" in line


def test_the_port_is_never_printed_without_a_chain(monkeypatch):
    """The whole point: a bare number is what let this go unnoticed."""
    for port in ("15715", "25715", "25779", "9876", "40000"):
        line = target_for(monkeypatch, port)
        assert line.strip() != f"rpc 127.0.0.1:{port}"
        assert "15715" in line, "every variant names mainnet so the reader can compare"
