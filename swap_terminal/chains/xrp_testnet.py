#!/usr/bin/env python3
"""The XRP TESTNET endpoint, its one RPC call, and the faucet accounts on disk.

Role: submodule (it talks to a rippled server and reads the key directory; the
      ONE decision it still holds is "which key in a faucet file is the secret".
      "IS THIS NETWORK A TEST NETWORK" WAS THE SECOND ONE AND IS NO LONGER MADE
      HERE, as of 2026-10-11: refuse_mainnet() derives it from
      chains/xrp_signing.require_non_mainnet(), because the copy that lived here
      had drifted from that one and passed a MISSING network_id. The drift and
      the measurement are at that function.)
Reads: the rippled JSON-RPC endpoint below, and
       ~/.config/swap_terminal/keys/xrp-testnet-*.json
Writes: nothing
Can move funds: no. It reads. Its callers sign and submit.
Mainnet-safe: yes, and it is what MAKES the callers mainnet-safe --
      refuse_mainnet() is the check every XRP script runs before submitting.

WHY THIS FILE EXISTS: THE ENDPOINT WAS SPELLED FOUR TIMES.

`TESTNET_URL = "https://s.altnet.rippletest.net:51234/"` appeared in
xrp_send_tagged.py, xrp_payout_verify.py and xrp_chain_check.py, and a fourth
copy was about to be written into the escrow harness. That is rule 8's failure
shape on the one constant where drift is worst: a script pointed at a different
server than the one refuse_mainnet() interrogated would have its safety check
answered by a machine that is not the machine it is about to submit to.

xrp_payout_verify.py already imported saved_faucet_accounts FROM
xrp_send_tagged.py, so root-script-imports-root-script was the existing habit.
That works and it puts a submodule's contents in a file an operator runs, which
is rule 10 backwards: a file at the root is an entry point, and three other
files importing it means it is also a library.

Nothing here changed behavior when it moved. The faucet-key search, its printed
explanation of which key matched, and the mainnet refusal are the same code,
measured the same way -- see the key table below, which is the record of the bug
that made the search necessary.
"""

from __future__ import annotations

import json
from pathlib import Path

import requests
from chains.xrp_signing import require_non_mainnet

TESTNET_URL = "https://s.altnet.rippletest.net:51234/"
KEY_DIRECTORY = Path.home() / ".config" / "swap_terminal" / "keys"


def rpc(method: str, params: dict) -> dict:
    """One rippled call. params is a LIST of one object; errors arrive as HTTP 200."""
    response = requests.post(
        TESTNET_URL,
        headers={"Content-Type": "application/json"},
        data=json.dumps({"method": method, "params": [params]}),
        timeout=40,
    )
    response.raise_for_status()
    payload = response.json()
    result = payload.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"{method}: no `result` object in the response")
    return result


# WHERE THE FAUCET PUTS THINGS, MEASURED rather than guessed. Dumped from the
# operator's own saved faucet files 2026-09-26, keys and string LENGTHS only so
# no secret was displayed:
#
#     account.xAddress        str, len 47
#     account.address         str, len 34
#     account.classicAddress  str, len 34
#     amount                  int = 100
#     transactionHash         str, len 64
#     seed                    str, len 31     <- THE SECRET, at the TOP level
#
# The first version looked for account.secret, payload.secret and account.seed.
# The real key is payload.seed -- one level off, so it found zero accounts and
# refused to send while two funded accounts sat in that directory. Exactly the
# shape of the `balance` vs `amount` bug in fund_testnets.py, which is the
# argument for searching a named list and REPORTING which key matched rather
# than hardcoding one guess.
ADDRESS_KEYS = ("address", "classicAddress")
SECRET_KEYS = ("seed", "secret", "master_seed", "secretKey")


def _first_present(keys: tuple[str, ...], *holders: dict):
    """The first of `keys` present in any of `holders`, with the key it came from."""
    for key in keys:
        for holder in holders:
            value = holder.get(key)
            if value:
                return value, key
    return None, None


def saved_faucet_accounts() -> list[tuple[Path, str, str]]:
    """Every (file, address, secret) in the key directory, newest first.

    The secret is returned because a caller has to sign with it. Nothing in this
    file ever prints it -- only the FILE NAME it came from, and the key name it
    was found under, both of which are safe and both of which are what made the
    original bug diagnosable.
    """
    found = []
    for path in sorted(KEY_DIRECTORY.glob("xrp-testnet-*.json"), reverse=True):
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        account = payload.get("account") or {}
        address, address_key = _first_present(ADDRESS_KEYS, account, payload)
        secret, secret_key = _first_present(SECRET_KEYS, payload, account)
        if address and secret:
            # Naming the two keys that matched is the whole point of searching a
            # list instead of hardcoding one. When the faucet changes shape again
            # this line says so on the next run, rather than the run reporting
            # zero accounts and leaving the reader to dump the files by hand --
            # which is what the original bug cost. Key NAMES only; the secret's
            # value is never printed here or anywhere else in this file.
            print(f"    {path.name}: address under {address_key!r}, "
                  f"secret under {secret_key!r} (value not shown)", flush=True)
            found.append((path, address, secret))
        elif address:
            print(f"    {path.name}: address under {address_key!r} but NO secret "
                  f"under any of {', '.join(SECRET_KEYS)} -- cannot sign with "
                  f"this one", flush=True)
        else:
            # Rule 14: an unusable file must not look identical to one this glob
            # never saw. Zero accounts with no explanation is the ambiguity the
            # original bug hid inside.
            print(f"    {path.name}: no address under any of "
                  f"{', '.join(ADDRESS_KEYS)} -- skipped", flush=True)
    return found


def refuse_mainnet() -> str:
    """Ask the server which network it is on, and refuse anything but a test one.

    ===========================================================================
    IT USED TO PASS A MISSING network_id, AND A STRINGIFIED ZERO, 2026-10-11
    ===========================================================================

    The test was `if network_id in MAINNET_NETWORK_IDS`, a membership check on a
    frozenset of ONE INTEGER, against whatever JSON put in that key. Measured
    against the two shapes that are not an integer 0:

        network_id ABSENT   None in frozenset({0}) is False  -> PASSED, and
                            returned the sentence "network_id None, build ..."
        network_id "0"      "0" in frozenset({0}) is False   -> PASSED as a
                            non-mainnet network

    chains/xrp_signing.require_non_mainnet() refuses all three -- a mainnet id, a
    MISSING id and an unreadable one -- and its docstring carries the argument for
    the second and third at length: "'I could not read the network' is not 'this
    is not mainnet'", which it calls rule 17's general form applied to the one
    question on this path where guessing wrong is unrecoverable.

    SO THERE WERE TWO COPIES OF ONE RULE AND THEY HAD DRIFTED, which is rule 8's
    shape on the guard this file's own header calls the thing that MAKES its
    callers mainnet-safe. Both copies read MAINNET_NETWORK_IDS, so they agreed
    about which id is mainnet and disagreed about every answer that is not an id.
    This one now DERIVES from the survivor rather than re-deciding: rule 8's "let
    the survivor own the concept", and chains/xrp_signing.py is the survivor
    because it is the copy on the swap payout path, the copy with the reasoning
    written down, and the copy with tests that disable each refusal in turn.

    WHAT THIS CHANGES FOR A CALLER, and it is strictly narrower rather than
    different. Every caller already treats this as "raises, or returns a sentence
    to print": xrp_send_tagged.py prints it, xrp_balances.py and
    atomic_swap_xrp.py wrap it in a console check that catches Exception. The
    refusal type is XRPMainnetRefused, which subclasses XRPSigningRefused and
    therefore RuntimeError -- the type this function already raised -- so a
    caller catching RuntimeError is unaffected.

    THE ABSENT CASE IS NOT HYPOTHETICAL IN SHAPE, AND WHETHER ANY rippled BUILD
    OMITS THE FIELD IS NOT MEASURED HERE (rule 17: say which you have). What IS
    measured: chains/xrp.py's own `network()` handles a missing network_id by
    falling back to build_version, which is a branch somebody wrote because the
    field can be absent, and tests/xrp_seeded_transport.server_info() takes
    network_id as a parameter for the same reason. What is NOT measured from this
    environment is which builds omit it -- no rippled was reachable.

    IT DOES NOT NEED TO BE MEASURED TO BE WORTH REFUSING, which is the whole of
    require_non_mainnet()'s argument and why deriving from it is the fix rather
    than adding a second condition here. This repository has already been bitten
    once by reading a field an upgrade removed: chains/base.RPCAdapter.
    _extract_matching_vouts() read scriptPubKey.addresses, gone from Bitcoin Core
    22.0, and every deposit_events row for weeks was written by the fabricated
    fallback beside it. A guard that answers "safe" for a field it could not read
    is that defect pointed at the irreversible direction.
    """
    info = rpc("server_info", {}).get("info") or {}
    # require_non_mainnet() RAISES on mainnet, on an absent id and on an
    # unreadable one, and returns its own description of what it established.
    #
    # THE MAINNET_NETWORK_IDS IMPORT IS GONE WITH THE COMPARISON (rule 9: every
    # time you are in a file, leave less of it behind). Proven dead before
    # deleting rather than assumed (rule 2): grepped the whole tree for the NAME,
    # and nothing imports it FROM this module -- tests/test_xrp_send_tagged.py
    # takes it from chains.xrp_signing, which is where it is defined and where the
    # decision now lives. The prose above still names it, which is the point of
    # naming it there.
    verdict = require_non_mainnet(info.get("network_id"), TESTNET_URL)
    return f"{verdict}, build {info.get('build_version')}"


