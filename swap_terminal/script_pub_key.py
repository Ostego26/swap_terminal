"""The one place that knows how a daemon spells a decoded output's address.

Role: shared leaf function (one decision -- which field an address is in)
Reads: nothing. Every input is an argument; no RPC, no file, no environment.
Writes: nothing
Can move funds: no. It never signs, sends or authorizes anything. It DOES
      decide whether a decoded output pays an address, and on the brokered
      Flask path that decision is one step upstream of a payout -- see
      chains/base.py's use of pays_address().
Mainnet-safe: yes -- pure dictionary reads, no I/O of any kind.

WHY THIS IS A MODULE AND NOT A LINE IN EACH CALLER.

Measured 2026-09-25 against real daemons, and this is the fact the whole file
exists for:

    Bitcoin Core 28.1.0    scriptPubKey keys: ['address', 'asm', 'desc', 'hex', 'type']
    Litecoin Core 0.21.4   scriptPubKey keys: ['addresses', 'asm', 'hex', 'reqSigs', 'type']

`addresses` (plural, a list) was DEPRECATED in Core 0.20 and REMOVED in 22.0.
`address` (singular, a string) replaced it. Litecoin 0.21.4 is four years
behind and still returns the old one. So code that reads one field finds
nothing on the other daemon -- and finding nothing is not an error, it is an
empty list, which reads as "this output does not pay that address".

THAT DEFECT HAS BEEN FOUND FOUR TIMES IN THIS TREE.

  1. modules/utils.wait_for_tx_output()      fixed 2026-09-25
  2. LTCClient.create_contract()'s inline copy of the same search
                                             fixed 2026-09-25
  3. regtest/steps.py's own vout search      it matches on the HEX and always
                                             did, which is why the harness
                                             could see the other two
  4. chains/base.RPCAdapter._extract_matching_vouts()
                                             the brokered Flask path, still
                                             reading `addresses` alone on
                                             2026-09-25 -- the fix that landed
                                             for the atomic-swap clients was
                                             never carried across, because
                                             nothing pointed from one to the
                                             other. That is rule 8 exactly:
                                             the copies agree on the day they
                                             are written, and nothing fails
                                             when one of them is corrected.

WHERE THIS LIVES, AND WHY NOT IN modules/.

modules/htlc_rpc.py already carried this knowledge, as address_of(), and it had
no production caller at all. The obvious move was to import it from
chains/base.py -- and that would make the Flask application unimportable
without `ecdsa`, `base58` and `bech32`, because modules/htlc_rpc imports
modules/htlc_spend which imports all three. swap_terminal/requirements.txt
states the opposite as a deployment property: the Flask app does not import the
atomic-swap path and can be deployed without its heavy dependencies. Measured
2026-09-25: chains/, services/, workers/, routes/, app.py and db.py import
NOTHING from modules/.

So it sits at the package root with zero third-party imports, which is exactly
what microfortnights.py is and for the same reason: a leaf both suites can
reach without either one dragging the other in.

MATCH ON THE HEX WHERE YOU CAN. This module is the fallback for code that only
has an address to compare against. Every comparison in this repository that
CAN be made on the scriptPubKey hex is -- modules/htlc_rpc.find_output_by_script
and assert_output_pays_the_contract both are -- because the hex is the same
bytes on every daemon and an address is a rendering that two chains do not even
agree on (Bitcoin and Litecoin use different base58 version bytes for P2SH).
"""

from __future__ import annotations

from collections.abc import Mapping

# What address_of() returns when the daemon named none. A sentence and not an
# empty string, because a blank gap in a log is ambiguous between "no address"
# and "the lookup broke" (rule 14).
NO_ADDRESS_REPORTED = "(none: this daemon reports no address for the output)"


def addresses_of(script_pub_key: Mapping | None) -> list[str]:
    """Every address a decoded scriptPubKey names, under EITHER daemon's shape.

    Reads `address` (Core 22.0 and later) and `addresses` (Core 0.19 and
    earlier, and Litecoin 0.21.4) and returns the union, so one call answers
    correctly on both without the caller knowing which daemon it is talking to.

    Returns a list, and an EMPTY list only when the daemon genuinely named no
    address -- a bare multisig or an OP_RETURN. It never returns empty merely
    because the caller guessed the wrong field name, which is the whole defect.

    A non-dict or None gives an empty list rather than raising: this is fed
    straight out of JSON that a daemon produced, and a caller walking a hundred
    outputs must not die on one odd shape. The caller cannot mistake the empty
    list for a real answer, because "no address" and "no match" lead to the
    same place -- the output is simply not credited.
    """
    if not isinstance(script_pub_key, Mapping):
        return []
    found: list[str] = []
    single = script_pub_key.get("address")
    if isinstance(single, str) and single:
        found.append(single)
    plural = script_pub_key.get("addresses")
    if isinstance(plural, (list, tuple)):
        found.extend(entry for entry in plural if isinstance(entry, str) and entry and entry not in found)
    return found


def pays_address(script_pub_key: Mapping | None, address: str) -> bool:
    """Does this decoded output pay `address`, on either daemon's field shape?

    THE DECISION, at the bottom, callable with seeded inputs (rule 10). An
    empty `address` is never a match: an output that names no address and a
    caller that was given no address must not agree with each other.
    """
    if not address:
        return False
    return address in addresses_of(script_pub_key)


def address_of(script_pub_key: Mapping | None) -> str:
    """The address a decoded scriptPubKey names, rendered for a human.

    FOR REPORTING ONLY. Nothing decides anything from this string -- a match is
    made by pays_address() or, better, on the scriptPubKey hex. It exists
    because an operator reading a pasted log wants the address, and joining the
    list here rather than at each call site keeps the rendering the same in
    every log line.
    """
    found = addresses_of(script_pub_key)
    return ", ".join(found) if found else NO_ADDRESS_REPORTED
