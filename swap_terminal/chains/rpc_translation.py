#!/usr/bin/env python3
"""The same question, asked of a chain that is not Bitcoin. One translator, one table.

Role: submodule (which protocol a chain speaks, which module speaks it, and one
      translated read -- each a decision callable with a stub adapter)
Reads: one chain adapter, passed in, and whichever command map a protocol names.
      It opens no socket of its own and knows no host, URL or credential.
Writes: nothing
Can move funds: no. Every name either map can translate is a READ by construction:
      each map's own test file asserts its tables against a denylist of everything
      that signs, submits or proposes a key, and NO_EQUIVALENT can only REMOVE
      names. A map cannot add a write, which is what makes "the map is the
      allowlist" a safety claim rather than a convenience.
Mainnet-safe: yes to import and to call. Nothing here signs or submits. Whether the
      endpoint it is pointed at is a test network is the adapter's question --
      chains/xrp.py refuses a mainnet network_id in server_parameters(), and
      network_target.solana_cluster() names a mainnet genesis hash.
Live-safe: yes.

=============================================================================
EXTRACTED FROM regtest/operator_panel.py ON 2026-10-10, FOR THE SECOND CALLER.
=============================================================================

Operator, 2026-09-30: "xrp control commands that are congruent to btc/ltc/grc rpc
commands since xrp is just different". That is what chains/xrp_rpc_map.py and
chains/solana_rpc_map.py answer, and until today the only thing that could ASK them
was the regtest panel's RPC console -- a dropdown of method names behind a POST.

/admin's chain panel needs the identical translation for the opposite reason: it
names no method at all. It asks the four questions a Core wallet asks (what is my
balance, did that arrive, am I connected, is this node current) and needs them
answered on a chain that has no Core wallet. The questions are the same, the maps
are the same, and the five steps -- refuse an unmapped name, build the call, catch a
missing argument, send, report -- are the same five. So this is one implementation
with two callers rather than two implementations (rule 8); regtest/operator_panel.py
keeps the two functions that take its own `ChainTab`, since a tab is that panel's
shape and not a chain fact.

WHAT WAS LEFT BEHIND, named so a reader of either file can find the other:

  console_protocol(tab)       takes a ChainTab and reads CONSOLE_PROTOCOL below. The
                              TABLE is a chain fact and moved; the tab-shaped
                              question is the regtest panel's.
  refuse_an_rpc_console(tab)  same reason, and its sentence is about that panel's
                              own Run buttons.

NOTHING ABOUT THE BEHAVIOR MOVED WITH THE TEXT. call_translated_read_only() is byte
for byte the function the panel has been calling, including its three-way
refused/needs/failed distinction, and tests/test_operator_panel.py exercises it
through this module's name now rather than through the panel's.
"""

from __future__ import annotations

from importlib import import_module

#: WHICH PROTOCOL EACH CHAIN'S CONSOLE SPEAKS. Three values and they are not interchangeable.
#:
#:   "bitcoin"   {"method": ..., "params": [positional]} against a bitcoind-family daemon.
#:               The allowlist is chains/daemon_wallet.READ_ONLY_RPCS.
#:   "xrpl"      {"method": ..., "params": [{named}]} against rippled. The allowlist is
#:               that protocol's own map tables -- see console_methods() for why the
#:               Bitcoin one cannot serve here.
#:   ""          no console. Said in words, with what to use instead.
#:
#: ONLY THE CHAINS THAT ARE NOT BITCOIN-FAMILY HAVE A ROW. The bitcoin-family three are
#: DERIVED by their caller -- regtest/operator_panel.console_protocol() from the tab's kind,
#: services/chain_panel.panel_kind() from chains/daemon_capabilities.BITCOIN_FAMILY -- because a
#: hand-kept row for BTC, LTC and GRC would be three chances to forget one (rule 8). XRP and SOL
#: are rows because their protocol is a fact about those chains and not about anything derivable.
#:
#: ICP IS IN NEITHER, AND THAT IS A POSITIVE FACT RATHER THAN A GAP. It does not speak JSON-RPC
#: at all: chains/icp.py reaches the ledger by shelling out to `dfx` with a Candid argument, so
#: there is no `{method, params}` to translate INTO and no map to write. A row here would be
#: claiming a translation exists. services/chain_panel.py renders that as its own named panel
#: kind, with the reason, rather than as a chain whose console failed.
CONSOLE_PROTOCOL = {"XRP": "xrpl", "SOL": "solana"}

#: WHICH MODULE SPEAKS EACH PROTOCOL. One entry per non-bitcoin console, and the ONLY place a
#: protocol name is turned into an implementation.
#:
#: Both modules present the SAME interface -- CONGRUENT, NO_EQUIVALENT, NATIVE_ONLY,
#: equivalent_of(), refuse_without_equivalent(), call_for(), MissingArgument -- and they were
#: made to, on 2026-09-30, at the moment the second one appeared. They did not start that way:
#: the XRP module had XRP_ONLY and xrp_call_for, the Solana one SOL_ONLY and solana_call_for,
#: and two modules answering the same four questions under different names means every reader
#: of both needs a branch. The branch IS the copy (rule 8), and merging at the moment the
#: second implementation lands is the only time it costs nothing.
CONSOLE_MAPS = {"xrpl": "chains.xrp_rpc_map", "solana": "chains.solana_rpc_map"}


def console_map(protocol: str):
    """The module that speaks this protocol, imported on demand. None if there is no console.

    IMPORTED BY NAME FROM ONE TABLE rather than by an `if protocol == ...` chain, so adding a
    chain's console is a row in CONSOLE_MAPS and nothing else. Deferred, like every other
    chains/ import in the regtest panel, so that panel still imports on a host without the
    mapped package's dependencies.
    """
    name = CONSOLE_MAPS.get(protocol)
    return None if name is None else import_module(name)


def console_methods(protocol: str) -> list[str]:
    """Every name this protocol's console offers, Bitcoin-style names first.

    THE MAP IS THE ALLOWLIST ON A TRANSLATED TAB, and that is a deliberate difference from the
    bitcoin-style tabs rather than a gap in READ_ONLY_RPCS. Two reasons, and the second decides
    it:

      READ_ONLY_RPCS is a list of BITCOIN method names. `fee` and `account_lines` on rippled,
      `getHealth` and `getSupply` on Solana, are not bitcoin names and should not be added to
      it -- putting them on the bitcoind allowlist would mean a GRC tab could be asked for
      `getSupply`, a method that daemon has never heard of, and that list would then be lying
      about what it governs.

      Every entry in either map IS a read, by construction, and each map's own test file
      asserts it against a denylist of everything that signs, submits or proposes a key. So a
      map can only ever SHRINK what a console offers -- NO_EQUIVALENT removes names, and
      nothing in either module can add a write.

    Sorted within each half rather than interleaved, because "what does this translate to" and
    "what can this chain tell me that Bitcoin cannot" are different questions and a single
    alphabetical list answers neither.
    """
    module = console_map(protocol)
    if module is None:
        return []
    return [*sorted(module.CONGRUENT), *sorted(module.NATIVE_ONLY)]


def call_translated_read_only(adapter, protocol: str, method: str, argument: object = None,
                             account: str = "") -> dict:
    """One translated read on a non-bitcoin chain. {ok, result} or {ok: false, error}. NEVER raises.

    ONE IMPLEMENTATION FOR EVERY TRANSLATED CONSOLE, and it was one function for XRP alone for
    about two hours. Writing the Solana one revealed it would have been the same five steps --
    refuse an unmapped name, build the call, catch a missing argument, send, report -- against a
    different module, which is rule 8's shape exactly: two copies agreeing on the day they are
    written. The protocol picks the module and nothing else differs.

    THE SAME THREE-WAY DISTINCTION call_read_only() makes, because it is the same distinction
    and collapsing it costs the same thing:

      refused=True   this name has no equivalent on that chain, or is not mapped. Stop looking,
                     or look where the message says.
      refused=True   with `needs`, when the call translates but an argument is missing. NOT the
                     same as the above -- ask again WITH it, and the message says which.
      refused=False  the chain answered, and the answer is an error. Its own words, unparaphrased.

    `translated` rides on every successful answer, because the operator asked for
    `getblockcount` and the node answered about `getBlockHeight`, and a reader who cannot see
    which method produced a figure cannot check it (rule 14: echo the parameters that decide the
    answer).

    THE PARAMS SHAPE IS THE MAP'S, NOT THIS FUNCTION'S. rippled takes one object and Solana
    takes a positional list, so call_for() returns whichever that chain wants and the adapter is
    handed it the way that adapter expects -- a dict as one argument, a list spread. That branch
    is here, once, and it is about a CALLING CONVENTION rather than about a policy.
    """
    module = console_map(protocol)
    if module is None:
        return {"ok": False, "refused": True,
                "error": f"there is no command map for protocol {protocol!r}, so nothing can be "
                         f"translated. CONSOLE_MAPS knows {', '.join(sorted(CONSOLE_MAPS))}."}

    refusal = module.refuse_without_equivalent(method)
    if refusal:
        return {"ok": False, "refused": True, "error": refusal}
    try:
        chain_method, params = module.call_for(method, argument, account)
    except module.MissingArgument as error:
        return {"ok": False, "refused": True, "needs": True, "error": str(error)}
    entry = module.equivalent_of(method)
    try:
        result = adapter.call(chain_method, params) if isinstance(params, dict) else adapter.call(chain_method, *params)
    except Exception as error:  # noqa: BLE001 -- checked: the failure IS the return value, named with its type, and a panel that dies on an account that does not exist is a panel that cannot be used to explore. rippled reports errors with HTTP 200 and a Solana node answers null for a skipped slot, so a chain-level error is the ordinary case here rather than the exceptional one.
        return {"ok": False, "refused": False, "translated": chain_method,
                "error": f"{type(error).__name__}: {error}"}
    return {"ok": True, "result": result, "translated": chain_method,
            "params": params, "answers": entry.answers, "note": entry.note}

