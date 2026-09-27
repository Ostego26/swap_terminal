#!/usr/bin/env python3
"""Read a funded HTLC off a chain: which output is it, and what preimage did the claim reveal.

Role: submodule (two decisions -- which vout holds the contract, and what the claim
      transaction's scriptSig was -- both answered from the chain and never assumed)
Reads: a daemon, through a `call(method, *args)` callable the caller supplies. No file,
      no environment, no database.
Writes: nothing. Both functions are read-only and neither builds or broadcasts anything.
Can move funds: no. What it returns DECIDES a fund movement -- a wrong vout sends a claim
      at the wrong output and a missed scriptSig loses the preimage -- so it is
      fund-critical while being unable to move anything itself.
Mainnet-safe: yes. Two RPC reads.
Live-safe: yes.

WHY THIS FILE EXISTS: TWO DRIVERS NEEDED THE SAME TWO READS.

Both functions were written inside atomic_swap_xrp_grc.py, where they served the Gridcoin
leg of one swap. A second driver for GRC<->LTC needs both, on both of its legs, and
copying them would be rule 8's exact shape: two copies that agree on the day they are
written and drift from then on, each reading correctly in its own file. They are moved
here rather than duplicated, and the XRP driver imports them, so there is one
implementation and one place a fix lands.

They are chain-generic already, which is what made the move safe: neither mentions XRP or
Gridcoin, both take a `call` callable, and both return (answer, explanation) so a polling
caller can say "not yet" per attempt instead of raising. Nothing about them changed in the
move -- the docstrings below are the originals, including the incident dates they cite,
because those incidents are why the functions are shaped as they are.

THE CALLABLE'S SHAPE, and it differs between this repo's two adapter styles. The XRP
driver's Gridcoin adapter exposes `call(method, *args)`. The atomic_*_client classes expose
`rpc_call(method, params_list)` instead. `client_caller()` below is the one-line adapter
between them, so a caller passes a callable and neither style leaks in here.
"""

from __future__ import annotations

from collections.abc import Callable


def client_caller(client) -> Callable:
    """Wrap an atomic_*_client so its rpc_call(method, [args]) looks like call(method, *args).

    One line, and it exists so the two functions below never learn that this repo has two
    adapter conventions. Putting the difference here rather than in each function is rule
    8's "if they genuinely differ, the difference is the point and belongs in ONE place".
    """
    def call(method: str, *args):
        return client.rpc_call(method, list(args))
    return call


class _CallableAdapter:
    """Give a bare callable the `.call` attribute the moved functions expect.

    The functions were written against an object and are moved UNCHANGED, which is what
    makes the move verifiable -- so the shim goes here rather than editing their bodies.
    """

    __slots__ = ("call",)

    def __init__(self, call: Callable) -> None:
        self.call = call


def adapter_for(call: Callable) -> _CallableAdapter:
    """The object to pass as `adapter` when all you have is a callable."""
    return _CallableAdapter(call)


def claim_scriptsig_hex(adapter, txid: str) -> tuple[str, list[str]]:
    """The claim transaction's input scriptSig, by whichever route answers.

    Returns (hex, reasons_tried). An empty hex with reasons is a result, not an
    exception -- the caller is polling and needs to say "not yet" per attempt.

    TWO ROUTES, FOR THE REASON modules/htlc_rpc.lookup_contract_output() HAS
    FOUR. `getrawtransaction` searches only the MEMPOOL unless the daemon runs
    -txindex, which is the exact defect that killed the BTC redeem path on
    2026-09-25 and cost a whole run to diagnose. Right after a broadcast the
    claim is in the mempool and route 1 answers; once it is mined it may not be
    findable that way at all, and this is the ONE step where failing is worst --
    both legs are funded and the secret is already public, so a participant who
    cannot read it has published nothing and lost the race to a timeout.

    Route 2 is the wallet: `gettransaction` returns the raw hex for any
    transaction the wallet knows, mined or not, with no -txindex, and
    `decoderawtransaction` turns it into the same shape. It works here because
    the claim was made by this wallet. A REAL participant is not the claimer and
    would not have it in their wallet -- for them route 1 plus -txindex, or a
    block scan, is the answer, and that is named here rather than discovered
    later.
    """
    reasons: list[str] = []
    try:
        raw = adapter.call("getrawtransaction", txid, 1) or {}
        script_sig = ((raw.get("vin") or [{}])[0].get("scriptSig") or {}).get("hex", "")
        if script_sig:
            return script_sig, reasons
        reasons.append("getrawtransaction: answered with no vin[0].scriptSig.hex")
    except Exception as error:  # noqa: BLE001 -- checked: the daemon answers "No information available about transaction" without -txindex once the claim is mined, which is not a failure but the signal to try the wallet. The reason is kept and printed rather than discarded, and a failure of BOTH routes returns "" which the caller reports as a FAIL -- never as "no preimage was revealed".
        reasons.append(f"getrawtransaction: {type(error).__name__}")
    try:
        wallet_tx = adapter.call("gettransaction", txid) or {}
        raw_hex = wallet_tx.get("hex")
        if not raw_hex:
            reasons.append("gettransaction: answered with no `hex`")
            return "", reasons
        decoded = adapter.call("decoderawtransaction", raw_hex) or {}
        script_sig = ((decoded.get("vin") or [{}])[0].get("scriptSig") or {}).get("hex", "")
        if script_sig:
            return script_sig, reasons
        reasons.append("decoderawtransaction: no vin[0].scriptSig.hex")
    except Exception as error:  # noqa: BLE001 -- checked: same, and this is the last route. Returning "" is reported by the caller as a failure to READ, which is a different thing from reading successfully and finding no preimage -- the caller prints the reasons so an operator can tell them apart.
        reasons.append(f"gettransaction/decoderawtransaction: {type(error).__name__}")
    return "", reasons


def htlc_vout(adapter, txid: str, p2sh_script_hex: str) -> tuple[int | None, str]:
    """Which output of the funding transaction IS the HTLC. Never assumed.

    Returns (vout, explanation). A None vout with an explanation is a result the
    caller reports; it is never defaulted to 0.

    WHY THIS EXISTS, and it is the same defect this repository fixed on the
    Bitcoin side on 2026-09-25. Gridcoin's `createhtlc` returns p2sh_address,
    redeem_script, sender_pubkey, receiver_pubkey, hash, timeout and txid -- and
    NO VOUT (read from src/rpc/htlc.cpp, 2026-09-26). It funds through
    SendMoney(), which adds a CHANGE output, so the HTLC is at index 0 or 1
    depending on coin selection. The first version of this file passed
    `int(htlc.get("vout", 0))` to claimhtlc, which is a guess about which output
    holds a real balance.

    MATCHED ON THE scriptPubKey HEX, not on a rendered address. That is the other
    half of the same 2026-09-25 lesson: `scriptPubKey.addresses` was removed in
    Bitcoin Core 22.0 and daemons disagree about whether it exists, while the hex
    is the same bytes everywhere. The hex here is derived from the redeem script
    the daemon itself returned, so a mismatch means the funding transaction does
    not pay the contract the daemon just described -- which is a refusal, not an
    index to fall back on.
    """
    reasons: list[str] = []
    for method, args in (("getrawtransaction", (txid, 1)), ("gettransaction", (txid,))):
        try:
            answer = adapter.call(method, *args) or {}
        except Exception as error:  # noqa: BLE001 -- checked: getrawtransaction answers "No information available about transaction" without -txindex once mined, which is the signal to try the wallet route, not a failure. Reasons are collected and returned rather than discarded, and a failure of both yields a None vout that the caller reports as a FAIL -- never a vout of 0.
            reasons.append(f"{method}: {type(error).__name__}")
            continue
        outputs = answer.get("vout")
        if outputs is None and answer.get("hex"):
            try:
                outputs = (adapter.call("decoderawtransaction", answer["hex"]) or {}).get("vout")
            except Exception as error:  # noqa: BLE001 -- checked: same; the wallet gave hex and the decode is the only step left. A failure is collected, not swallowed.
                reasons.append(f"decoderawtransaction: {type(error).__name__}")
                continue
        for entry in outputs or []:
            if ((entry.get("scriptPubKey") or {}).get("hex", "")).lower() == p2sh_script_hex.lower():
                return int(entry.get("n", -1)), f"matched scriptPubKey {p2sh_script_hex} via {method}"
        reasons.append(f"{method}: read {len(outputs or [])} outputs, none paying {p2sh_script_hex}")
    return None, "; ".join(reasons) or "no route answered"
