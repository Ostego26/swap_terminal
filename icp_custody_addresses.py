#!/usr/bin/env python3
"""Show the canister's threshold key beside the desk's current addresses. READ ONLY.

Role: file (entry point -- an operator runs this and reads the two columns)
Reads: threshold_custody.public_key via dfx, and each configured chain adapter's
       own_address(). Nothing else.
Writes: NOTHING. No file, no database row, no key, no address.
Can move funds: no, and structurally. It calls one canister query-shaped update
       that returns a PUBLIC key, and one read-only RPC per chain. There is no
       send path in this file and nothing it prints can be spent.
Live-safe: yes, and the read-only claim is checked rather than asserted --
       chains/base.RPCAdapter.own_address() uses listreceivedbyaddress and
       getaddressesbylabel, never getnewaddress, and its own docstring says why
       ("getnewaddress would answer in one call and DERIVES a key"). So running
       this a hundred times creates nothing in any wallet.

WHY THIS EXISTS AND WHAT IT DELIBERATELY DOES NOT DO. icp/threshold_custody holds
one secp256k1 key inside the replica's threshold ECDSA subnet, and
modules/pubkey_address.py turns a compressed public key into a P2PKH address for
BTC, LTC or GRC. So ONE key can be the desk's address on three chains, with no
private key on any disk -- which is the strongest thing the ICP half of this
system offers.

Making those the desk's ACTUAL addresses moves where funds live. That is armed
state, it is the operator's call and not mine (rule 16), and this file exists
precisely so the comparison can be looked at before anybody decides. It prints two
columns and changes nothing.

WHAT A READER MUST NOT CONCLUDE FROM THE OUTPUT. The key is `dfx_test_key`, which
exists only inside this container: it is a local replica's own threshold key and
controls nothing on any real chain. The mainnet addresses printed below are
DERIVATIONS of it, not accounts with a balance, and the testnet addresses for
BTC, LTC and GRC are IDENTICAL to each other because all three chains use version
byte 0x6F for testnet P2PKH -- measured, and the reason a testnet address alone
never tells you which chain it is for.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from config import Config
from modules.pubkey_address import PublicKeyRefused, address_from_public_key

from swap_terminal.chains.icp import ICPCallFailed, dfx_transport
from swap_terminal.chains.registry import build_adapters, missing_settings

#: The canister that holds the key. Its id is environment state -- every fresh
#: replica issues a different one -- so it is read from the environment with no
#: default, for the reason config.py's ICP entry gives: a plausible default is the
#: dangerous kind.
_CANISTER_VARIABLE = "ICP_THRESHOLD_CUSTODY_CANISTER_ID"

#: `public_key_hex = "03..."` out of dfx's candid record. Matched rather than
#: parsed as candid, and a miss RAISES below rather than yielding an empty key.
_PUBKEY = re.compile(r'public_key_hex\s*=\s*"([0-9a-fA-F]+)"')

#: Which chains a threshold secp256k1 key can be an address on through
#: modules/pubkey_address. Not SOL or XRP: those are ed25519 and a different
#: derivation entirely, so they are absent rather than printed as unsupported.
_P2PKH_CHAINS = ("BTC", "LTC", "GRC")


def canister_public_key(canister_id: str, service: str, timeout: float, call=None) -> str:
    """The compressed public key hex from threshold_custody. Raises if unreadable.

    An empty derivation path, which is the canister's root key -- the same
    argument the operator ran by hand. A regex miss raises rather than returning
    "" because an empty key would derive three addresses that look real.

    `call` IS INJECTED for the reason chains/icp.ICPAdapter's is: the parsing is
    the decision here and it has to be callable with seeded text, or the only way
    to test it is to have a replica (rule 10). It defaults to the dfx transport,
    and the default is deliberately the same one the adapter uses rather than a
    second implementation of "run dfx in compose" (rule 8).
    """
    transport = call if call is not None else dfx_transport(service, timeout)
    out = transport(canister_id, "public_key", "(vec {})")
    found = _PUBKEY.search(out)
    if not found:
        raise ICPCallFailed(
            f"public_key on {canister_id} returned text with no public_key_hex in it, so NO key "
            f"was read (this is not an empty key): {out.strip()[:300]!r}"
        )
    return found.group(1).lower()


def desk_addresses(adapters: dict) -> dict[str, str]:
    """Each configured chain's current desk address, or a reason it has none.

    Never raises on one chain's behalf: a daemon that is down must not stop the
    comparison for the two that are up, and the reason is printed in the cell
    rather than swallowed (rule 14 -- an empty gap is ambiguous between "no
    address" and "the query broke").
    """
    found = {}
    for asset in _P2PKH_CHAINS:
        adapter = adapters.get(asset)
        if adapter is None:
            found[asset] = f"(no adapter -- set {', '.join(missing_settings(Config.RPC, asset)) or 'its RPC variables'})"
            continue
        try:
            address = adapter.own_address()
        except Exception as error:  # noqa: BLE001 -- checked: a diagnostic must not die on one chain's daemon being down, and the handler does NOT return a value a reader could mistake for an address -- it returns the error text, prefixed, into a column labeled as the desk's address
            found[asset] = f"(unreadable: {type(error).__name__}: {error})"
            continue
        found[asset] = address or "(none -- the wallet has no address to read)"
    return found


def main() -> int:
    """Announce the target first, then print both columns. Rule 14 throughout."""
    canister_id = os.environ.get(_CANISTER_VARIABLE, "")
    icp = Config.RPC.get("ICP") or {}
    service = icp.get("service") or "icp-replica"
    timeout = float(icp.get("timeout") or 60.0)
    network = Config.NETWORK if hasattr(Config, "NETWORK") else "testnet"

    print("canister threshold key vs the desk's current addresses -- READ ONLY, nothing is changed")
    print(f"  canister   {canister_id or f'(unset -- export {_CANISTER_VARIABLE})'}")
    print(f"  dfx service {service}  timeout {timeout}s")
    print(f"  network    {network}  <- which column of P2PKH_VERSION_FOR is authoritative below")
    if not canister_id:
        print(
            f"\nREFUSED, nothing read: {_CANISTER_VARIABLE} is unset. Its value is environment "
            f"state -- every fresh replica issues a different canister id -- so there is no "
            f"default worth guessing. Read it from the replica with:\n"
            f"  docker compose -f docker-compose.yml -f docker-compose.icp.yml exec -T "
            f"icp-replica cat /repo/.dfx/local/canister_ids.json",
            file=sys.stderr,
        )
        return 2

    try:
        public_key_hex = canister_public_key(canister_id, service, timeout)
    except ICPCallFailed as error:
        print(f"\nREFUSED, nothing compared: {error}", file=sys.stderr)
        return 1

    key = bytes.fromhex(public_key_hex)
    print(f"\npublic key   {public_key_hex}")
    print(f"             {len(key)} bytes, prefix 0x{key[0]:02x}  <- 33 and 0x02/0x03 is a compressed secp256k1 point")

    adapters = build_adapters(Config.RPC)
    current = desk_addresses(adapters)

    print(f"\n{'chain':6} {'FROM THE CANISTER KEY':36} {'THE DESK TODAY':36} same?")
    for asset in _P2PKH_CHAINS:
        try:
            derived = address_from_public_key(key, asset, network)
        except PublicKeyRefused as error:
            derived = f"(refused: {error})"
        today = current[asset]
        same = "YES" if derived == today else "no"
        print(f"{asset:6} {derived:36} {today:36} {same}")

    print(
        "\nEvery 'no' above is EXPECTED and is not a fault: the desk's addresses were generated by "
        "its own wallets and the canister's were derived from a key that has never signed anything. "
        "A 'YES' would mean the two already coincide."
    )
    print(
        f"NOTHING WAS CHANGED. Moving the desk to a canister-derived address would move where funds "
        f"live, which is armed state and the operator's decision -- this file only shows the "
        f"comparison. The key is a LOCAL replica key and controls nothing on any real chain, so the "
        f"{network} addresses above are derivations rather than accounts with a balance."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
