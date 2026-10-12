#!/usr/bin/env python3
"""Every RPC method this tree calls must have a KNOWN answer for every chain.

Role: tests (read-only)
Reads: chains/rpc_matrix.py, chains/gridcoin_rpc_commands.py, chains/xrp_rpc_map.py,
       chains/solana_rpc_map.py, and rpc_matrix.py through conftest.root_entry_point().
       No network: every cell is release-source data.
Writes: nothing
Can move funds: no
Mainnet-safe: yes

WHY THIS FILE EXISTS, IN THE OPERATOR'S WORDS.

2026-10-12: "why do you keep fucking choking on gridcoin? it's a 2014 coin. it's not
like ltc or btc. it's similar but DIFFERENT. stop assuming."

They were describing a pattern, not a slip. Four separate times this session, Gridcoin
was reasoned about as Bitcoin Core and corrected by its source: -rpcbind does not exist
there at all; it uses split config files rather than [test] sections; its rpcallowip
does not read CIDR; and setting rpcallowip WIDENS the bind, which is the opposite of
Core. The structural reason is that "Bitcoin-derived" was this tree's unwritten default
-- GRC is in daemon_capabilities.BITCOIN_FAMILY and shares chains/base.RPCAdapter -- and
an unwritten default cannot be wrong.

THE ASSUMPTION WAS BACKWARDS IN BOTH DIRECTIONS, which is the finding that makes the
matrix worth maintaining rather than reading once:

    fundrawtransaction    Core 0.10+              GRC: ABSENT
    getaddressesbylabel   Core 0.17+              GRC: ABSENT (getaddressesbyaccount)
    signrawtransaction    REMOVED in Core 0.18    GRC: PRESENT
    createhtlc            never in Core           GRC: PRESENT

So it is not an older subset. A method works on the 2014 coin and not on the modern one,
and Gridcoin has command categories Core has never had.

WHAT THIS GATE IS FOR. Both live-path absences were reached through the SHARED adapter
and swallowed by a broad `except` that reported the METHOD name, so neither had ever
been noticed. Rule 12 names that shape exactly: a broad catch "is never legitimate when
the caller cannot tell the failure from a real answer." A test is the only thing that
notices in advance.
"""

from __future__ import annotations

import pytest
from chains import gridcoin_rpc_commands, rpc_matrix, solana_rpc_map, xrp_rpc_map
from chains.rpc_matrix import ASSETS, cell
from conftest import root_entry_point

printer = root_entry_point("rpc_matrix.py", "rpc_matrix_entry")

#: The chains whose column is derived from Bitcoin Core's own version history. The GRC
#: column is NOT one of them -- that is the whole point.
CORE_STYLE = ("BTC", "LTC")


def test_every_method_this_tree_calls_has_a_known_answer_for_every_chain():
    """THE GATE. An UNKNOWN cell is a method nobody has established, not a working one.

    The rows are DERIVED by grepping this tree for `.call("...")` on the live path, so
    the denominator cannot go stale: adding a call to an adapter adds a row here, and if
    no authority knows that method the cell is UNKNOWN and this fails.

    UNKNOWN IS THE FAILURE AND ABSENT IS NOT. A method Gridcoin does not have is a fact
    to design around -- chains/base.measure_send_fee() calling fundrawtransaction on GRC
    is handled, badly but knowingly. A method nobody has looked up is the state this
    session kept being in.
    """
    methods = printer.called_methods()
    assert methods, "the scan found no .call() sites at all, so this gate measures nothing"

    unknown = [
        (method, asset)
        for method in methods
        for asset in ASSETS
        if cell(asset, method).state == rpc_matrix.UNKNOWN
    ]

    assert not unknown, (
        f"no authority knows whether these methods exist on these chains: {unknown}. Add the "
        f"method to chains/rpc_matrix.ARRIVED_IN / ANCIENT / REMOVED_IN for a Core-style "
        f"chain, or to chains/gridcoin_rpc_commands.CORE_ONLY for GRC. An UNKNOWN cell is "
        f"how a Core-only method gets called on a 2014 coin and swallowed by a broad except."
    )


def test_the_subset_assumption_is_WRONG_IN_BOTH_DIRECTIONS():
    """The inversion, pinned, so nobody simplifies it back to "GRC is older Core".

    `signrawtransaction` was deprecated in Core 0.17 and removed in 0.18 -- confirmed
    against v28.1's src/rpc/client.cpp, where only the ...withkey and ...withwallet
    spellings appear. Gridcoin 5.5.1.0 still dispatches the original. So the direction of
    the difference depends on the method, and any code that branches on "is this chain
    Bitcoin-derived" to decide which spelling to send will be wrong half the time.
    """
    assert cell("GRC", "signrawtransaction").state == rpc_matrix.PRESENT
    for asset in CORE_STYLE:
        assert cell(asset, "signrawtransaction").state == rpc_matrix.ABSENT, (
            f"{asset} is Core {rpc_matrix.CORE_BASE[asset]}, which is past the 0.18 removal"
        )

    # And the other direction, on the same row set.
    assert cell("GRC", "fundrawtransaction").state == rpc_matrix.ABSENT
    for asset in CORE_STYLE:
        assert cell(asset, "fundrawtransaction").state == rpc_matrix.PRESENT

    # And a whole category Core never had.
    assert cell("GRC", "createhtlc").state == rpc_matrix.PRESENT
    assert cell("BTC", "createhtlc").state == rpc_matrix.ABSENT


def test_core_versions_compare_as_TUPLES_and_not_as_strings():
    """Core went 0.9 ... 0.21 and then jumped to 22.0, so "0.21" > "22.0" as a string.

    A string comparison here would report every Core 22+ method as present on Litecoin
    0.21.4 -- quietly, and in the direction that invents capability. generateblock
    (Core 22.0) and getdeploymentinfo (Core 25.0) are the two rows that catch it.
    """
    assert rpc_matrix.CORE_BASE["LTC"] < (22, 0) < rpc_matrix.CORE_BASE["BTC"]
    assert cell("BTC", "generateblock").state == rpc_matrix.PRESENT
    assert cell("LTC", "generateblock").state == rpc_matrix.ABSENT
    assert cell("BTC", "getdeploymentinfo").state == rpc_matrix.PRESENT
    assert cell("LTC", "getdeploymentinfo").state == rpc_matrix.ABSENT


@pytest.mark.parametrize("method", sorted(gridcoin_rpc_commands.COMMANDS))
def test_the_GRC_column_never_contradicts_gridcoins_own_table(method):
    """NO SECOND OPINION. The matrix joins; it does not re-decide.

    This is services/pair_view.py's oldest lesson in a different file: /admin and the
    customer page disagreed about one pair in one process because each computed its own
    verdict. Every command in Gridcoin's dispatch table must read PRESENT here, for all
    166 of them, or the join has started holding facts of its own.
    """
    entry = cell("GRC", method)

    assert entry.state == rpc_matrix.PRESENT, (
        f"{method} is in Gridcoin {gridcoin_rpc_commands.SOURCE_TAG}'s own command table and "
        f"the matrix says {entry.state}"
    )
    assert entry.equivalent == method, "a present method is called by its own name"


@pytest.mark.parametrize("asset,module", [("SOL", solana_rpc_map), ("XRP", xrp_rpc_map)])
def test_the_SOL_and_XRP_columns_come_from_the_maps_that_already_existed(asset, module):
    """Those two maps were written at the operator's instruction on 2026-09-30.

    The matrix reads them. Asserted by comparing every congruent entry against the cell,
    so a fact restated in rpc_matrix.py rather than read from the map would show up as a
    mismatch rather than as a duplicate nobody noticed (rule 8).
    """
    assert module.CONGRUENT, f"{asset}'s map is empty, so this gate measures nothing"
    for bitcoin_method, equivalent in module.CONGRUENT.items():
        entry = cell(asset, bitcoin_method)
        assert entry.state == rpc_matrix.RENAMED, f"{asset}/{bitcoin_method}"
        assert entry.equivalent == equivalent.method, (
            f"{asset}/{bitcoin_method}: the matrix says {entry.equivalent!r} and the map says "
            f"{equivalent.method!r}. The matrix must READ the map, never restate it."
        )


def test_ICP_is_NOT_RPC_for_everything_rather_than_absent():
    """A chain that speaks a different protocol is not a chain missing a method.

    ABSENT invites "add it"; NOT_RPC says the question does not apply, because
    chains/icp.py reaches the ledger through `dfx canister call` against Candid and there
    is no method NAME to map. Collapsing the two would put ICP in the disagreement list
    on every single row and bury the four that matter.
    """
    for method in ("getbalance", "sendtoaddress", "signrawtransaction", "createhtlc"):
        entry = cell("ICP", method)
        assert entry.state == rpc_matrix.NOT_RPC
        assert "Candid" in entry.why
    assert rpc_matrix.disagreements("getbalance") == "", (
        "ICP must not make a row disagree: only BTC, LTC and GRC are compared"
    )


def test_the_disagreement_list_is_SHORT_and_holds_the_real_hazards():
    """The report's value is that the useful half is the short half (rule 14).

    Measured 2026-10-12: 16 live-path methods, 4 disagreeing. If every row disagreed the
    list would be the table again and nobody would read it; if none did, the module
    would be measuring nothing. Both failures are asserted against rather than assumed.
    """
    methods = printer.called_methods()
    disagreeing = [method for method in methods if rpc_matrix.disagreements(method)]

    assert disagreeing, "no row disagrees, so either the join broke or the chains converged"
    assert len(disagreeing) < len(methods) / 2, (
        f"{len(disagreeing)} of {len(methods)} rows disagree; the summary has become the table"
    )
    # THE FOUR THAT WERE LIVE-PATH HAZARDS when this was written. Named, because each is
    # a call this tree makes that cannot work on GRC, and a regression that silently
    # removed one from the list would remove the warning rather than the hazard.
    for method in ("fundrawtransaction", "getaddressesbylabel", "listwalletdir"):
        assert method in disagreeing, f"{method} is a GRC absence this tree calls anyway"


def test_the_printer_contacts_nothing_and_always_exits_zero(capsys):
    """It reports facts about the chains, not a verdict about this host.

    A non-zero exit would make `rpc_matrix.py && something` unusable for the one thing it
    is for, and -- unlike pair_verdicts.py, whose exit code IS a verdict about a desk --
    a disagreement here is not a fault to fix.
    """
    assert printer.main([]) == 0
    printed = capsys.readouterr().out

    assert "WHERE THE THREE BITCOIN-STYLE CHAINS DISAGREE" in printed
    assert gridcoin_rpc_commands.SOURCE_URL in printed, "the source must be citable from the output"
    assert "NOT a claim" in printed and "BINARY" in printed, (
        "the output must state that no cell is a claim about the operator's daemon"
    )
    # EVERY CHAIN IS A COLUMN, including the ones with nothing interesting to say -- a
    # chain missing from the table is indistinguishable from one nobody mapped.
    for asset in ASSETS:
        assert asset in printed
