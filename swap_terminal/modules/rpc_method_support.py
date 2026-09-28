#!/usr/bin/env python3
"""Which JSON-RPC methods each Bitcoin-derived daemon actually HAS, and how loudly to report a miss.

Role: function level (one decision -- a log level and a sentence, from a method name and an error)
Reads: nothing. Both inputs are arguments. No chain, no file, no environment.
Writes: nothing.
Can move funds: no. It decides only how a failure is REPORTED. Every caller re-raises
       whatever it was given, so nothing here can change what a client does with an error --
       only what an operator reads about it.
Mainnet-safe: yes. It opens nothing and never sees an RPC parameter, which is deliberate: the
       thing it formats goes into a log, and the params of `walletpassphrase` are a
       passphrase and the params of a redeem carry a preimage. Only the METHOD NAME is taken.

WHY THIS EXISTS: RULE 14, BACKWARDS. THE THING THAT WORKED LOOKED BROKEN.

Measured on the operator's Gridcoin testnet daemon 2026-09-27, and recorded at length in
modules/htlc_rpc.lookup_contract_output(), which names this file as where the fix belongs:

    `help gettxout`  ->  "unknown command: gettxout"

Gridcoin does not have `gettxout`. It is not a misconfiguration and not a version skew --
Gridcoin sits on the pre-0.17 Bitcoin RPC surface and the method was never there. So route 1
of lookup_contract_output()'s four routes ALWAYS misses on GRC, every single spend, and the
client's `except Exception: logger.exception(...)` printed the full exception chain at ERROR
in front of a spend that then SUCCEEDED via route 4.

    measured, same run    lines of traceback printed before a SUCCESSFUL GRC spend    ~40
                          lines printed for a 401 (a real failure)                   ~100

An operator reading that pastes it back and asks what went wrong. Nothing went wrong. Rule 14
says a poll that did nothing must not look like one that did work; this is the mirror -- work
that SUCCEEDED must not look like a failure, and forty lines of stack is the loudest way to
say "failure" that a terminal has.

WHY NOT SIMPLY LOWER THE LEVEL ON EVERYTHING. Because a 401, a refused connection and a
rejected transaction are all real, and they arrive through the same `except`. The distinction
that has to be drawn is "this method does not exist on this chain, and a caller is PROBING for
it" versus everything else, and only the first is routine.

WHY A TABLE AND NOT JUST THE ERROR CODE. `-32601` alone would quiet a method-not-found on ANY
method, including one this repository genuinely needs -- `signrawtransaction` missing would
mean a daemon that cannot sign, and that must stay loud. So the code AND the method name are
both required, and the table says which chains each optional method is expected on. A
`-32601` on a method not in the table stays at ERROR with its traceback.

ONE COPY, THREE CLIENTS (rule 8). atomic_btc_client.py and atomic_grc_client.py each have the
blanket handler this feeds; atomic_ltc_client.py does not (measured 2026-09-28: it catches
RequestException only, so a `-32601` there propagates with no logging at all -- quieter than
either sibling and for no stated reason). Putting the rule here means the three cannot drift
about which misses are routine, which is the failure mode this repository keeps paying for.
"""

from __future__ import annotations

import logging

# JSON-RPC's own code for "no such method". Spelled once, as an int, because the string
# "-32601" also matches a timestamp, an amount and a txid substring.
METHOD_NOT_FOUND = -32601

# Methods a caller PROBES for, and the chains that have them. A miss on one of these is an
# expected answer rather than a fault, and the value is what the sentence names so an operator
# reading the line learns the fact rather than just being told to ignore it.
#
# MEASURED, not recalled. `gettxout` was checked with `help gettxout` on the operator's
# Gridcoin testnet daemon 2026-09-27 ("unknown command: gettxout") while
# `getrawtransaction`, `gettransaction` and `signrawtransaction` all answered.
# modules/htlc_rpc.lookup_contract_output() is the caller, and its route 4 is what covers the
# miss -- which is why a miss costs one round trip and nothing else.
#
# A method belongs here ONLY IF A CALLER ALREADY HAS A FALLBACK FOR IT. That is the whole
# license for quieting the line: the work still gets done. Adding a method here that no
# caller falls back from would hide a real failure, which is rule 19's definition of a patch
# -- stopping the symptom being reported rather than the cause existing.
OPTIONAL_PROBE_METHODS: dict[str, tuple[str, ...]] = {
    "gettxout": ("BTC", "LTC"),
}


def is_method_not_found(error: BaseException) -> bool:
    """True when this exception is a JSON-RPC -32601 and not something that merely mentions it.

    modules/htlc_rpc.rpc_result() raises a bare `Exception` whose message embeds the daemon's
    whole error object --

        GRC RPC Error: {'code': -32601, 'message': 'Method not found'}

    -- so the CODE is the only structured thing available and it is available only as text.
    That is why this reads the repr of the code as a key/value pair rather than searching for
    the bare number: `-32601` on its own would also match a timestamp or an amount, and a
    substring match on a daemon's free-text message is how a reporting rule starts firing on
    unrelated errors.

    Reading it out of a string is NOT the shape this wants, and the better fix is upstream --
    rpc_result() raising a typed error carrying `code` as an int. That file is held by another
    agent in this session, so this is what can be done from here without a conflicting edit,
    and it is named as a proposal rather than presented as the design.
    """
    text = str(error)
    return f"'code': {METHOD_NOT_FOUND}" in text or f'"code": {METHOD_NOT_FOUND}' in text


def rpc_failure_report(asset: str, method: str, error: BaseException) -> tuple[int, str, bool]:
    """(log level, sentence, include_traceback) for an RPC call that raised.

    Three values because the three move together and a caller that decided any of them
    locally would be the fourth place this rule lives. `include_traceback` is separate from
    the level on purpose: `logger.exception()` is ERROR *and* a stack, and the expensive part
    of the 2026-09-27 output was the stack rather than the level.

    THE ROUTINE CASE -- a probe for a method this chain does not have -- comes back at DEBUG
    with no traceback, and the sentence NAMES THE CHAINS THE METHOD IS EXPECTED ON, because
    "gettxout is missing" invites an operator to go install something. It is not missing; it
    was never there.

    EVERYTHING ELSE comes back at ERROR with a traceback, unchanged. A 401, a refused
    connection, a rejected transaction and a -32601 on a method this repository actually needs
    all land here, and all four should be as loud as they were.

    The sentence carries NO PARAMETERS, only the method name. modules/htlc_rpc.
    describe_rpc_payload() exists because this same line used to print the payload verbatim,
    which on GRC meant the wallet passphrase and on a redeem means the preimage.
    """
    expected_on = OPTIONAL_PROBE_METHODS.get(method)
    if expected_on is not None and is_method_not_found(error):
        return (
            logging.DEBUG,
            f"{asset} has no `{method}`; it is expected on {' and '.join(expected_on)}. This call is a PROBE "
            f"with a fallback behind it, so it continues by another route and NOTHING IS WRONG. "
            f"Reported at debug since 2026-09-28: on GRC this miss happens on EVERY spend, and printing "
            f"it at error with a stack made a spend that SUCCEEDED read as a failure",
            False,
        )
    return logging.ERROR, f"{asset} RPC call `{method}` failed: {error}", True
