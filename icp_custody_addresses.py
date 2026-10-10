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

THE KEY CHANGES WHEN THE REPLICA IS RECREATED, AND THAT IS MEASURED. Same canister
id, same `dfx_test_key` name, two different keys across one container recreation on
2026-10-07:

    before   038b01b0b6d5f09ce5375c30453fcda4ea7e9b451d3b72b7bc185ee2dbe4c6a8e6
    after    03508d617633f3ddf921aa52f23ab17db64da9d1680a29ef342768c6fab258075a

So every address this file prints is a derivation of a key that does not survive
`docker compose down`. Funds sent to a canister-derived address on a LOCAL replica
become unreachable the next time the container is replaced -- not "probably", and not
only on a rebuild: the recreation that produced the two keys above was an ordinary
stop-and-start cycle.

THIS IS A PROPERTY OF dfx_test_key AND NOT OF CANISTER CUSTODY. Mainnet's threshold
key (`key_1`) is held by a subnet and outlives any single canister or replica, which
is the whole premise of the design. What it means is narrower and still sharp: THE
LOCAL REPLICA CANNOT BE USED TO VALIDATE DURABILITY. A local run proves the
derivation works and proves nothing about whether an address persists, and anybody
reading this output must treat the addresses as throwaway.

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
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from config import Config
from microfortnights import format_duration
from modules.address_authority import expected_network
from modules.address_network import TESTNET, address_network, decode_segwit_address
from modules.keyring_paths import (
    CURVE_FOR_ASSET,
    ROOT_SECP256K1,
    KeyRequest,
    KeyringRefused,
    request_for,
)
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
#:
#: FIVE NAMES, ONE MEMBERSHIP, AND THEY ARE NOT THE SAME CONCEPT (rule 8: "If
#: they genuinely differ, the difference is the point and belongs in a comment at
#: BOTH sites, naming the other one"). Counted 2026-10-09:
#:
#:   wallet_custody.SCRIPT_CHAINS                 custody is by WALLET
#:   modules/atomic_swapper.SUPPORTED_ASSETS      pairs the script swapper can do
#:   show_payout_fees.MEASURABLE                  payout fee measurable from here
#:   icp_custody_addresses._P2PKH_CHAINS          has legacy P2PKH addresses
#:   chains/daemon_capabilities.BITCOIN_FAMILY    has a Bitcoin Core release
#:
#: They agree today and can diverge -- a bech32-only Bitcoin fork would be in
#: SCRIPT_CHAINS and not in _P2PKH_CHAINS -- so they are NOT merged.
#: tests/test_daemon_capabilities.py asserts they agree now, which makes a future
#: divergence deliberate rather than an accident nobody notices.
_P2PKH_CHAINS = ("BTC", "LTC", "GRC")


def candid_blob(raw: bytes) -> str:
    """One derivation-path element as a candid blob literal.

    EVERY BYTE IS HEX-ESCAPED, including the printable ones. `blob "swap_terminal"`
    is valid candid and would read better, but the path also carries a four-byte
    big-endian index whose bytes are mostly unprintable and one of which is
    routinely 0x00 -- and a literal that is readable for some elements and escaped
    for others is a literal whose encoding a reader has to work out per element.
    Uniform escaping has one rule and no edge case.

    A PURE FUNCTION because this is the decision (rule 10): an element encoded
    wrongly is a different derivation path, which is a different key, which is a
    different address. tests/test_keyring_paths.py asserts the exact text.
    """
    return 'blob "' + "".join(f"\\{b:02x}" for b in raw) + '"'


def candid_public_key_argument(request: KeyRequest) -> str:
    """The candid text for threshold_custody's `public_key` argument.

    THE CANISTER'S ARGUMENT BECAME A RECORD IN INCREMENT 1b, and this function is
    where that shape lives. It used to be the bare `(vec {})` spelled at the one
    call site -- fine while there was one curve and one path, and wrong the moment
    there were three curves, because a curve passed positionally cannot be
    checked and a missing one cannot be defaulted safely.

    IT TAKES A `KeyRequest` RATHER THAN TWO ARGUMENTS, mirroring the canister's
    own record. The curve was validated when the request was constructed, so
    there is nothing left to check here and no way to pair one asset's curve with
    another's path in between.
    """
    elements = "; ".join(candid_blob(bytes(e)) for e in request.derivation_path)
    path = f"vec {{ {elements} }}" if elements else "vec {}"
    return (
        f"(record {{ curve = variant {{ {request.curve} }}; derivation_path = {path} }})"
    )


def canister_public_key(
    canister_id: str,
    service: str,
    timeout: float,
    call=None,
    request: KeyRequest = ROOT_SECP256K1,
) -> str:
    """The public key hex from threshold_custody for `request`.

    THE DEFAULT IS STILL THE ROOT KEY ON secp256k1 -- an empty derivation path,
    the same argument the operator ran by hand -- so this function answers the
    same question it answered before increment 1b and the output of this file is
    unchanged for anyone who does not ask for more. A regex miss raises rather
    than returning "" because an empty key would derive three addresses that look
    real.

    THE DEFAULT IS NOT THE PER-ASSET PATH, and that is deliberate rather than
    unfinished. The root key is ONE key, which is what makes this file's output a
    single key over three addresses. Per-asset paths give three unrelated keys,
    which is a different table; `--per-asset` prints that one and says so.

    `call` IS INJECTED for the reason chains/icp.ICPAdapter's is: the parsing is
    the decision here and it has to be callable with seeded text, or the only way
    to test it is to have a replica (rule 10). It defaults to the dfx transport,
    and the default is deliberately the same one the adapter uses rather than a
    second implementation of "run dfx in compose" (rule 8).
    """
    transport = call if call is not None else dfx_transport(service, timeout)
    out = transport(canister_id, "public_key", candid_public_key_argument(request))
    found = _PUBKEY.search(out)
    if not found:
        raise ICPCallFailed(
            f"public_key on {canister_id} returned text with no public_key_hex in it, so NO key "
            f"was read (this is not an empty key): {out.strip()[:300]!r}"
        )
    return found.group(1).lower()


def keyring_plan(networks: dict[str, str], assets: Sequence[str]) -> list[dict]:
    """What `--per-asset` will ask the canister for, decided before anything is asked.

    A PURE FUNCTION AND THEREFORE THE TESTABLE PART (rule 10). It resolves each
    asset's curve and derivation path from modules/keyring_paths and returns rows;
    it opens nothing and asks nothing. An asset with no keyring curve, or on a
    network the scheme will not name, becomes a row carrying its REFUSAL rather
    than being dropped -- rule 14's "never let an empty result print nothing",
    applied per row: a chain missing from the table would otherwise look like a
    chain that was fine.
    """
    rows = []
    for asset in assets:
        row: dict = {"asset": asset, "network": networks.get(asset, "(unknown)")}
        try:
            request = request_for(asset, row["network"])
            row["request"] = request
            row["curve"] = request.curve
            row["path_text"] = request.describe()
        except KeyringRefused as error:
            row["refused"] = str(error)
        rows.append(row)
    return rows


def print_per_asset_keyring(
    networks: dict[str, str], canister_id: str, service: str, timeout: float
) -> int:
    """Ask for one key per asset at its own path, and print the table. Returns an exit code.

    EXTRACTED FROM main() rather than written inline, because inline it pushed
    main() to 60 statements against a ceiling of 50 -- and CLAUDE.md rule 12 says
    what to do about that: "a main() past the ceiling is orchestration that has
    swallowed decisions. The fix is to extract the decision so it can be called
    with seeded inputs, not to raise the ceiling." The decision this holds is
    which failures are fatal to the run, and `keyring_plan` above holds the one
    about what to ask for.

    THE EXIT CODE IS NON-ZERO IF ANY ROW FAILED, which is a deliberate difference
    from the default report. The default report treats a chain whose daemon is
    down as information and still exits 0; here a failed row means the keyring
    could not answer for an asset, and a caller scripting this needs to know that
    without parsing the table.
    """
    print("\nPER-ASSET KEYRING (modules/keyring_paths). One key per asset, not one key for all.")
    print(
        "  These are DIFFERENT keys from the root key above and therefore different addresses.\n"
        "  Nothing here is armed: no desk address changes and no funds move."
    )
    print(f"\n{'chain':6} {'curve':17} {'derivation path':52} key")
    failures = 0
    for row in keyring_plan(networks, sorted(CURVE_FOR_ASSET)):
        if "refused" in row:
            failures += 1
            print(f"{row['asset']:6} {'(refused)':17} {row['refused'][:52]:52} -")
            continue
        try:
            key_hex = canister_public_key(
                canister_id, service, timeout, request=row["request"]
            )
        except (ICPCallFailed, KeyringRefused) as error:
            failures += 1
            # NAMED, NOT SWALLOWED, and the curve is in the line because that is
            # the field that decides which management call was made -- a schnorr
            # key the replica does not have fails here and nowhere else.
            print(f"{row['asset']:6} {row['curve']:17} {row['path_text']:52} FAILED: {error}")
            continue
        print(f"{row['asset']:6} {row['curve']:17} {row['path_text']:52} {key_hex}")

    # "did nothing" MUST NOT LOOK LIKE "did work" (rule 14). A table of five
    # FAILED rows and a table of five keys would otherwise end the same way.
    total = len(CURVE_FOR_ASSET)
    print(
        f"\n{total - failures} of {total} per-asset keys were read"
        f"{'' if failures == 0 else f'; {failures} FAILED and are named above'}."
    )
    return 0 if failures == 0 else 1


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


def comparability(derived: str, today: str) -> tuple[str, str]:
    """(verdict, why) for one row. THE DECISION this file exists to report.

    WHY THIS IS NOT `derived == today`. The first version of this file printed a
    bare `no` for every row plus one blanket sentence saying the addresses differ
    because the desk's were generated by its own wallets. Run against the
    operator's live host 2026-10-06, that sentence was wrong for two of three rows:

        BTC   mp7vKdjG...8vNJKE   bcrt1qpee23n6wr8ypn35z3w5v5hmndkvc0nwmcxg7lh   no
        LTC   mp7vKdjG...8vNJKE   rltc1qz6j5ja8mgtu79lngdz7sy4wyay2lr3y6g846ja   no
        GRC   mp7vKdjG...8vNJKE   mrJBUvRGjfPzdbBRBNSPJxAtFUmFr6UipK             no

    The desk's BTC and LTC addresses are bech32 SegWit and modules/pubkey_address
    derives LEGACY P2PKH. Those two can never be equal, so `no` there does not mean
    "a different address" -- it means "not comparable", and only the GRC row was an
    actual comparison. One explanation covering both is a wrong statement printed to
    an operator who is reading it before an armed-state decision, which rule 16
    calls a bug and not a wording preference.

    decode_segwit_address().hrp is the discriminator, and modules/address_network
    owns it rather than this file sniffing for a '1' -- that module already carries
    the HRP table including `rltc`, which was MISSING there until 2026-09-27 and is
    exactly the kind of gap a second copy here would reintroduce (rule 8).
    """
    if derived == today:
        return "SAME", "the desk is already on this canister-derived address"
    segwit = decode_segwit_address(today)
    if segwit.hrp is not None:
        return "n/a", (
            f"not comparable: the desk holds a bech32 SegWit address (hrp '{segwit.hrp}') and "
            f"this key derives LEGACY P2PKH. Moving the desk here would also change its address "
            f"TYPE, not only its key"
        )
    if not today or today.startswith("("):
        return "n/a", "no desk address to compare against -- see that cell"
    derived_network, derived_why = address_network(derived)
    today_network, today_why = address_network(today)
    if derived_network != today_network:
        return "n/a", f"different networks: derived {derived_why}; desk {today_why}"
    return "differs", f"both P2PKH on {today_network}, and they are different addresses"


def networks_for(rpc: dict) -> dict[str, str]:
    """Which network each P2PKH chain's address is derived against. THE decision.

    EXTRACTED FROM main() ON 2026-10-09, AND THE EXTRACTION IS THE POINT RATHER
    THAN TIDINESS. This was one line inside a sixty-line orchestration, which is
    rule 10's defect exactly: the only way to exercise it was to run the whole
    tool, so the test written for it tested `expected_network()` directly instead
    -- and PASSED against the broken version, which never called that function at
    all. The mutation caught the test, not the code. A decision that cannot be
    called with seeded inputs is a decision nothing can be wrong about.

    WHAT IT REPLACED, and it had never worked:

        network = Config.NETWORK if hasattr(Config, "NETWORK") else "testnet"

    config.Config has no NETWORK attribute and nothing in the tree sets one, so
    the hasattr was always False and the value was always the literal "testnet",
    for every chain. It reads as "respects the configured network, defaulting to
    testnet" -- a sentence about a setting that does not exist.

    The cost was not cosmetic. `network` picks the version byte
    (modules/pubkey_address.P2PKH_VERSION_FOR, keyed (asset, network): GRC mainnet
    0x3E, GRC testnet 0x6F), so on a host with GRC on 15715 and LTC on 9332 --
    both MAINNET ports -- all three chains derived the SAME testnet address from
    one key, comparability() reported "different networks", and every row came
    back n/a with nothing on screen saying why.

    TESTNET WHEN IT IS NOT ESTABLISHED, and that direction is deliberate.
    expected_network() answers None for an unconfigured port AND for an
    unrecognized one -- its own docstring refuses to call a mainnet daemon on a
    custom -rpcport "not mainnet". Guessing MAINNET would print an address in the
    format an operator might FUND; these are derivations of a local dfx_test_key
    that controls nothing on any real chain, so a visibly throwaway address is the
    cheaper way to be wrong.
    """
    return {asset: expected_network(asset, rpc) or TESTNET for asset in _P2PKH_CHAINS}


def main() -> int:
    """Announce the target first, then print both columns. Rule 14 throughout."""
    canister_id = os.environ.get(_CANISTER_VARIABLE, "")
    # NO .get() AND NO CONVERSIONS: config.IcpRpc declares `service` a `str` and
    # `timeout` a `float`, and config.py defines both for every process
    # (_env("ICP_DFX_SERVICE", "icp-replica") and _env_float("ICP_CALL_TIMEOUT",
    # "60")), so the `or` fallbacks and the float() were there to get `object`
    # values past canister_public_key()'s signature rather than to defend against
    # anything config.py can produce.
    icp = Config.RPC["ICP"]
    service = icp["service"]
    timeout = icp["timeout"]

    # `Config.NETWORK` DID NOT EXIST AND HAD NEVER EXISTED. Until 2026-10-09 this
    # line read
    #
    #     network = Config.NETWORK if hasattr(Config, "NETWORK") else "testnet"
    #
    # and the hasattr() was the whole defect: config.Config has no NETWORK
    # attribute, nothing in this tree sets one, so the condition was always False
    # and the value was always the literal "testnet". A reader -- and I did read it
    # this way -- takes that line as "respects the configured network, defaulting to
    # testnet", which is a sentence about a setting that does not exist. The
    # expression was its own documentation and the documentation was false.
    #
    # WHICH MATTERS BECAUSE `network` PICKS THE VERSION BYTE. It is passed to
    # modules/pubkey_address.address_from_public_key(), which keys
    # P2PKH_VERSION_FOR on (asset, network): GRC/mainnet is 0x3E and GRC/testnet is
    # 0x6F, so the wrong value here does not fail -- it prints a well-formed address
    # for the other network, and comparability() below then reports "different
    # networks" and the row says nothing. On a host whose daemons are on mainnet
    # ports, every row of this report was an n/a for a reason nothing on screen
    # named.
    #
    # DERIVED PER ASSET FROM THE ONE AUTHORITY, not from a constant and not from a
    # second port table. modules/address_authority.expected_network() answers
    # "which network does this process believe it is on for this asset" from the
    # chain's configured RPC port through network_target.classify(), which is the
    # same function the workers' startup banner, swap_readiness and the address
    # checks all use (rule 8/11). Per asset rather than once, because the three
    # chains are three independent daemons: nothing stops BTC being on regtest while
    # GRC is on mainnet, and address_from_public_key() already takes the network per
    # call.
    #
    # TESTNET WHEN IT IS NOT ESTABLISHED, and that direction is deliberate.
    # expected_network() returns None for an unconfigured port and for an
    # unrecognized one -- its own docstring refuses to call a custom -rpcport "not
    # mainnet" -- and this file's header is emphatic that these addresses are
    # derivations of a LOCAL dfx_test_key that controls nothing on any real chain.
    # Guessing mainnet would print an address in the format an operator might fund;
    # guessing testnet prints one that is visibly throwaway, which is the cheaper
    # way to be wrong.
    #
    # dict(Config.RPC) because expected_network() takes a plain `dict` and
    # config.RPC is a TypedDict (config.RpcSettings), which the typing spec lets a
    # caller read generically only as a Mapping. Same entries, same objects: it
    # reads one port per asset and nothing else.
    table = dict(Config.RPC)
    networks = networks_for(table)

    print("canister threshold key vs the desk's current addresses -- READ ONLY, nothing is changed")
    print(f"  canister   {canister_id or f'(unset -- export {_CANISTER_VARIABLE})'}")
    # format_duration() RATHER THAN f"{timeout}s", 2026-10-09. CLAUDE.md rule 6:
    # every timing this system REPORTS is in microfortnights, with the seconds in
    # parentheses where an operator may also need to read the env var that sets it
    # -- which is exactly this line, against ICP_CALL_TIMEOUT. Measured while
    # fixing it: this was the ONLY `timeout {...}s` left outside tests/ in the
    # whole tree, and the other five root tools in this bucket (fund_desk,
    # swap_readiness, wallet_custody, solana_chain_check, pay_test_deposit) all
    # already import format_duration. One outlier, not a convention.
    print(f"  dfx service {service}  timeout {format_duration(timeout)}")
    print(f"  network    {', '.join(f'{a} {n}' for a, n in networks.items())}  <- the row of "
          f"P2PKH_VERSION_FOR each chain below is derived from, read from that chain's configured "
          f"RPC port. An unset or unrecognized port is NOT ESTABLISHED and falls back to testnet")
    if not canister_id:
        print(
            f"\nREFUSED, nothing read: {_CANISTER_VARIABLE} is unset. Its value is environment "
            f"state -- every fresh replica issues a different canister id -- so there is no "
            f"default worth guessing. Read it from the replica with:\n"
            f"  docker compose -f docker-compose.yml exec -T "
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
    # SAID ON THE SCREEN AND NOT ONLY IN THE HEADER (rule 14): an operator reads the
    # output, and the output is what gets pasted back a day later. Measured 2026-10-07
    # across one container recreation -- see this file's header for both key values.
    print("             NOT STABLE: a local replica issues a NEW dfx_test_key when its")
    print("             container is recreated, so every address below is THROWAWAY.")
    print("             Measured 2026-10-07: one stop-and-start changed this key.")
    print(f"             {len(key)} bytes, prefix 0x{key[0]:02x}  <- 33 and 0x02/0x03 is a compressed secp256k1 point")

    adapters = build_adapters(Config.RPC)
    current = desk_addresses(adapters)

    print(f"\n{'chain':6} {'FROM THE CANISTER KEY (legacy P2PKH)':38} {'THE DESK TODAY':44} verdict")
    verdicts = {}
    for asset in _P2PKH_CHAINS:
        try:
            derived = address_from_public_key(key, asset, networks[asset])
        except PublicKeyRefused as error:
            derived = f"(refused: {error})"
        today = current[asset]
        verdict, why = comparability(derived, today)
        verdicts[asset] = (verdict, why)
        print(f"{asset:6} {derived:38} {today:44} {verdict}")

    print("\nwhy each verdict, because the one-word column is not the answer:")
    for asset in _P2PKH_CHAINS:
        verdict, why = verdicts[asset]
        print(f"  {asset:4} {verdict:8} {why}")

    comparable = [a for a in _P2PKH_CHAINS if verdicts[a][0] in ("SAME", "differs")]
    print(
        f"\n{len(comparable)} of {len(_P2PKH_CHAINS)} rows were an actual comparison"
        f"{' (' + ', '.join(comparable) + ')' if comparable else ' -- NONE, so this run compared nothing'}."
        f" An `n/a` is not a mismatch; it means the two addresses are different KINDS and could"
        f" never be equal."
    )
    print(
        f"NOTHING WAS CHANGED. Moving the desk to a canister-derived address would move where funds "
        f"live, which is armed state and the operator's decision -- this file only shows the "
        f"comparison. The key is a LOCAL replica key and controls nothing on any real chain, so the "
        f"{'/'.join(sorted(set(networks.values())))} addresses above are derivations rather than "
        f"accounts with a balance."
    )

    # =====================================================================
    # THE PER-ASSET KEYRING, BEHIND A FLAG AND OFF BY DEFAULT
    # =====================================================================
    #
    # OFF BY DEFAULT FOR A MEASURED-FROM-ABSENCE REASON, which is the honest way
    # to put it: this section makes one canister call PER ASSET, and two of the
    # five assets are on ed25519, which reaches `schnorr_public_key` rather than
    # `ecdsa_public_key`. WHETHER A LOCAL dfx REPLICA PROVISIONS A SCHNORR KEY
    # NAMED dfx_test_key IS UNVERIFIED FROM HERE -- there is no dfx and no replica
    # in the container this was written in, so it could not be run (rule 17: a
    # reason to believe is not a check). If the local replica has no schnorr key
    # the SOL and XRP rows will fail, and that must not be able to break the
    # default run of a diagnostic the operator relies on. So the default output is
    # byte-for-byte what it was before increment 1b, and this is `--per-asset`.
    if "--per-asset" not in sys.argv:
        print(
            "\nper-asset keyring not shown. Re-run with --per-asset to ask the canister for "
            "one key per asset at its own derivation path (5 extra update calls; the two "
            "ed25519 rows need a schnorr key, which a local replica may not provision)."
        )
        return 0

    return print_per_asset_keyring(networks, canister_id, service, timeout)


if __name__ == "__main__":
    raise SystemExit(main())
